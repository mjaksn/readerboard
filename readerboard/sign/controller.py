"""The one thing allowed to talk to the sign.

Every byte that reaches the sign goes through here, behind a single lock. That
is what makes concurrent requests safe: the sign is one serial device with no
notion of interleaved conversations, so two callers arriving together must be
made to take turns rather than left to garble each other's packets. Callers
therefore need no delays or queueing of their own.

The controller also remembers the exact bytes it last put in each sign file and
declines to write them again. Sources tend to re-send on a schedule whether or
not anything changed, and every such write makes the sign visibly redraw.
Suppressing them is what stops the display flickering for no reason.

pyserial blocks, so the actual write happens on a worker thread. The lock is
held across the thread hop, so ordering is preserved.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.transport.base import Transport, TransportError

logger = logging.getLogger(__name__)

# How long to wait after clearing memory before writing the new configuration.
# An "E$" clear puts the BetaBrite Classic through a reset. The configuration
# that follows is accepted through it, apparently buffered and applied when the
# sign comes back. A configuration written a second or more after the clear was
# seen to display once the sign returned; one second was the shortest gap tried,
# so this sits a little above it for margin rather than at an edge. It is only
# paid on a reconfiguration, which is rare and already the one dangerous
# operation.
MEMORY_CLEAR_SETTLE_SECONDS = 2.0

# How long to wait for a reset to finish before writing to the sign again. Both
# of the protocol's resets pay it: the "E$" clear inside apply_memory_config, and
# the "E," soft reset a caller asks for by name through SOFT_RESET.
#
# apply_memory_config lets the configuration itself buffer through the reset,
# which the sign applies as it comes back, but message content needs the sign
# actually back and listening rather than merely able to hold one buffered write.
#
# The wait is always taken with the sign's lock held. That is the whole point of
# it: a write arriving while the sign is deaf is not refused, it is lost, because
# the transport accepts it, the suppression cache records it as delivered and the
# caller is told it worked. Holding the lock makes other writers queue instead.
# Paid only against real hardware, since a link told to pace nothing is a fake or
# a simulator, and neither has a reset to sit through.
RESET_SETTLE_SECONDS = 10.0

# How long the sign cannot hear anything after it has been asked to make a
# noise. The document is unusually direct about this, in two footnotes to the
# tone command in Table 15:
#
#   "the tone generation command must be the last transmission frame because the
#   sign's serial port is disabled (and cannot receive any data) while a tone is
#   generated"
#
#   "Wait a minimum of 3 seconds before transmitting more data to the sign"
#
# Both fixed sounds this service offers, the continuous tone and the three
# beeps, run for about two seconds, and the footnote asks for three. So a beep
# is not a reset and still leaves the sign deaf, which is why the wait below is
# not about restarting at all.
#
# Without it the next write goes out one inter_packet_delay later, half a second
# by default, into a sign that is not listening. It is not refused: the
# transport accepts it, the suppression cache records the file as holding those
# bytes, and nothing writes them again until the next periodic re-push.
SOUND_SETTLE_SECONDS = 3.0

# Reading a reply. The sign begins answering with a long run of nulls and the
# payload follows a moment behind, so a reader that takes what is waiting and
# stops gets a lone null byte back from every question it asks. Two of those
# compare equal, which looks like a confirmation and is not; that mistake was
# made once already against this sign. So a reply is collected until the line
# has been quiet for a few polls running.
#
# The numbers are polls rather than seconds so that a test can drive this with a
# sleep that records instead of waiting, and still exercise the same loop.
READ_POLL_SECONDS = 0.05
READ_QUIET_POLLS = 4
READ_TIMEOUT_SECONDS = 3.0

ReconnectHook = Callable[[], Awaitable[None]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SignController:
    """Owns the transport, serialises writes, and suppresses redundant ones."""

    def __init__(
        self,
        transport: Transport,
        *,
        inter_packet_delay: float = 0.5,
        settle: bool = True,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wrap a transport. Nothing is sent until :meth:`start`.

        ``settle`` is whether to sit out the windows in which the sign cannot
        listen, after a reset or a tone. Turn it off only for something that is
        not a sign: the simulator has no diagnostics to run and no speaker to
        switch its port off for, so waiting for it is twelve seconds of nothing
        on every start. It is deliberately not the same knob as
        ``inter_packet_delay``. That one is how fast this end may talk; a settle
        is how long the other end is deaf, which is a fact about the hardware
        and not about pacing.

        ``sleep`` is injected for the same reason ``now`` is: so a test can ask
        what the controller waited for rather than how long it actually took.
        Measuring elapsed wall time to check the inter-packet delay is a flaky
        thing to do, because a platform whose timer granularity is coarser than
        the delay can return from two of them in less than twice the delay, and
        the test then fails on a margin that says nothing about the code.
        """
        self._transport = transport
        self._inter_packet_delay = inter_packet_delay
        self._settle = settle
        self._now = now
        self._sleep = sleep

        self._lock = asyncio.Lock()
        self._file_contents: dict[bytes, bytes] = {}
        self._run_sequence: list[bytes] | None = None
        self._reconnect_hooks: list[ReconnectHook] = []
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

        self.writes = 0
        self.suppressed = 0
        self.last_write_at: datetime | None = None
        self.last_error: str | None = None

    # == lifecycle ==========================================================

    def on_reconnect(self, hook: ReconnectHook) -> None:
        """Register something to run each time the link comes back up.

        The clock sync uses this, and so does the registry's refresh. A sign
        that lost power has a wrong clock, and the link coming back is the
        closest signal the service gets to that having happened.
        """
        self._reconnect_hooks.append(hook)

    async def start(self) -> None:
        """Open the link if it will open, and begin watching it."""
        try:
            await self._connect()
        except TransportError as err:
            # Not fatal. The service should come up with the sign unplugged and
            # start working when it is plugged back in.
            logger.warning("sign not reachable at startup: %s", err)
        except Exception:
            # Nor is anything else. Whatever the first open did, the watcher
            # below is what brings the link back, and refusing to start the
            # service is a worse answer than starting it without a sign.
            logger.exception("opening the sign at startup failed unexpectedly")
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        """Stop watching the link and close it."""
        self._stopping.set()
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(self._transport.close)

    # == health =============================================================

    @property
    def is_connected(self) -> bool:
        """Whether the link to the sign is currently up."""
        return self._transport.is_open

    @property
    def link_description(self) -> str:
        """The configured link, for the health endpoint and log lines."""
        return self._transport.description

    def cached_labels(self) -> list[str]:
        """Which sign files the controller believes it has written."""
        return sorted(label.decode("ascii") for label in self._file_contents)

    def forget_sign_contents(self) -> None:
        """Stop believing anything about what the sign is currently holding.

        Suppression is only safe while the cache and the sign agree, and there
        is one way for them to disagree silently: the sign and the Ethernet
        adapter are separately powered, so the sign can be power cycled with the
        TCP link to the adapter still up. Nothing fires, the cache stays warm,
        and the next identical write, the one that would repair a blank sign, is
        the one suppression throws away.

        Calling this makes the next write of everything actually happen.
        """
        self._file_contents.clear()
        self._run_sequence = None

    # == writing ============================================================

    async def write_text_file(
        self,
        label: bytes,
        body: bytes,
        *,
        mode: bytes = c.MODE_HOLD,
        force: bool = False,
    ) -> bool:
        """Put ``body`` in a sign file. Returns False if the write was suppressed.

        ``force`` writes even when the bytes match what the sign is believed to
        hold. It is for the case where that belief is exactly what is in doubt,
        such as re-asserting an alert after the sign may have been power cycled.
        """
        payload = frames.write_text_file(label, body, mode=mode)
        if force:
            await self._send(payload)
            self._file_contents[label] = payload
            return True
        return await self._send_if_changed(label, payload)

    async def write_priority(
        self,
        body: bytes,
        *,
        mode: bytes = c.MODE_HOLD,
        force: bool = False,
    ) -> bool:
        """Take the display over with an alert.

        This never releases the display, whatever ``body`` holds. An empty body
        still carries the Start-of-Message byte, a position and a mode, and the
        sign reads that as a blank priority message it should show, so it keeps
        the screen. :meth:`clear_priority` sends the bare write a release needs.
        """
        return await self.write_text_file(
            c.FILE_PRIORITY, body, mode=mode, force=force
        )

    async def clear_priority(self, *, force: bool = False) -> bool:
        """Release a priority takeover so the run sequence resumes.

        Deliberately not ``write_priority(b"")``. An ordinary write to the
        priority file, even with an empty body, carries the Start-of-Message
        byte and a position and mode, and the sign reads that as a blank
        priority message it should display, keeping the screen instead of
        handing it back. The release is the bare write ``clear_priority_file``
        builds, and it goes through the same suppression cache under the
        priority label so a repeated release is not re-sent.
        """
        payload = frames.clear_priority_file()
        if force:
            await self._send(payload)
            self._file_contents[c.FILE_PRIORITY] = payload
            return True
        return await self._send_if_changed(c.FILE_PRIORITY, payload)

    async def set_run_sequence(self, labels: list[bytes]) -> bool:
        """Choose which files play and in what order. Returns False if unchanged."""
        if self._run_sequence == labels:
            self.suppressed += 1
            logger.debug("run sequence unchanged, not writing it again")
            return False
        payload = frames.set_run_sequence(labels)
        await self._send(payload)
        self._run_sequence = list(labels)
        return True

    async def apply_memory_config(self, allocations: list[frames.FileAllocation]) -> None:
        """Reallocate the sign's files.

        This erases everything already on the sign, so it is never suppressed
        and never done casually. Callers are expected to have checked that the
        pool actually changed before asking for it.
        """
        labels = ", ".join(entry.label.decode("ascii") for entry in allocations)
        logger.warning("reallocating sign memory (%s); this clears every message", labels)
        # Clear memory outright first. A BetaBrite Classic on an Ethernet to
        # RS-232 adapter was measured accepting a memory configuration, storing
        # it, echoing it back correctly on a read, and then displaying nothing
        # from any file it named. The one thing that made the files play was a
        # bare "E$" clear ahead of the configuration; with it the sign showed
        # the rotation, without it the sign stayed blank, on writes identical to
        # the byte. So the clear is not tidiness here, it is what makes the
        # configuration take. It adds no risk: writing a configuration already
        # erases the sign, which is why this whole method is the one dangerous
        # operation, so clearing first reaches the same empty sign by a
        # different door. See docs/protocol-notes.md.
        # The whole sequence runs under one acquisition of the lock. The sign is
        # deaf from the moment the clear reaches it until its diagnostics
        # finish, and a write arriving in that window is not refused, it is
        # simply gone: the transport accepts it, the suppression cache records
        # it as delivered, and the caller is told 200. Holding the lock makes
        # every other writer queue behind the reset instead. That is why the
        # sends below are ``_send_locked``; ``asyncio.Lock`` is not reentrant.
        async with self._lock:
            await self._send_locked(frames.clear_memory())
            # Let the reset the clear triggers finish before the configuration
            # lands, or the configuration arrives while the sign is deaf and is
            # lost.
            if self._settle:
                await self._sleep(MEMORY_CLEAR_SETTLE_SECONDS)
            await self._send_locked(frames.set_memory_config(allocations))
            # The configuration buffers through the reset, but message content
            # needs the sign actually back and listening rather than merely able
            # to hold one buffered write. Whoever rebuilds the display next is
            # holding this lock's queue, so waiting here is what stops their
            # writes landing on a sign that cannot say it missed them.
            if self._settle:
                await self._sleep(RESET_SETTLE_SECONDS)
        # The sign is now empty, so nothing we thought we knew about it holds.
        self.forget_sign_contents()

    async def send_special(self, payload: bytes, *, settle_seconds: float = 0.0) -> None:
        """Send a special function such as a clock command.

        Never suppressed. Setting the clock to the same value it already holds
        is still worth doing, because the point is to correct drift we cannot
        see.

        ``settle_seconds`` is for a command that leaves the sign unable to
        listen: a reset running its power-up diagnostics, or a tone, during
        which the protocol says the serial port is switched off. The wait is
        taken with the lock held, so the send and the wait are one
        uninterruptible step and another caller's write queues instead of being
        thrown at a sign that is deaf and cannot say so.

        Skipped only when the controller was built with ``settle`` false, which
        says the thing on the other end is not a sign. It is not tied to
        ``inter_packet_delay``: a sign paced as fast as the line allows is deaf
        for exactly as long after a tone as a slowly paced one.
        """
        async with self._lock:
            await self._send_locked(payload)
            if settle_seconds and self._settle:
                await self._sleep(settle_seconds)

    async def read_special(self, payload: bytes) -> bytes:
        """Ask the sign a question and collect the whole answer.

        Holds the lock across the send and the read, which is not optional. The
        reply is matched to the question by nothing but arriving next: the sign
        stamps its answer with the Response type code and an address it sends
        "regardless of the sign's actual address", so two reads in flight at
        once would be told apart by luck. Holding the lock is what makes "the
        next thing on the wire" mean "the answer to this".

        Raises :class:`TransportError` when the sign says nothing at all, which
        is the same class a failed write raises and reaches a caller as a 503.
        """
        async with self._lock:
            await self._send_locked(payload)

            reply = bytearray()
            quiet = 0
            polls = max(1, int(READ_TIMEOUT_SECONDS / READ_POLL_SECONDS))

            for _ in range(polls):
                try:
                    chunk = await asyncio.to_thread(self._transport.read_available)
                except TransportError:
                    raise
                except Exception as err:
                    raise TransportError(
                        "reading from %s failed: %s" % (self._transport.description, err)
                    ) from err

                if chunk:
                    reply += chunk
                    quiet = 0
                elif reply:
                    quiet += 1
                    if quiet >= READ_QUIET_POLLS:
                        break

                await self._sleep(READ_POLL_SECONDS)

            if not reply:
                raise TransportError(
                    "the sign at %s did not answer within %.1fs"
                    % (self._transport.description, READ_TIMEOUT_SECONDS)
                )

            logger.debug("read %d byte(s) from %s", len(reply), self._transport.description)
            return bytes(reply)

    # == internals ==========================================================

    async def _send_if_changed(self, label: bytes, payload: bytes) -> bool:
        if self._file_contents.get(label) == payload:
            self.suppressed += 1
            logger.debug(
                "file %s already holds these exact bytes, not writing it again",
                label.decode("ascii"),
            )
            return False

        await self._send(payload)
        self._file_contents[label] = payload
        return True

    async def _send(self, payload: bytes) -> None:
        async with self._lock:
            await self._send_locked(payload)

    async def _send_locked(self, payload: bytes) -> None:
        """Send one payload. The caller must already hold the lock.

        Split out of :meth:`_send` so that a sequence which must not be
        interleaved, such as the clear and configuration either side of a
        memory reset, can hold the lock across the whole of it.
        ``asyncio.Lock`` is not reentrant, so those callers cannot simply call
        :meth:`_send` again.
        """
        packet = frames.packet(payload)
        try:
            await asyncio.to_thread(self._transport.write, packet)
        except TransportError as err:
            self.last_error = str(err)
            # What we believed about the sign's contents may not have
            # survived a failed write, so stop believing it.
            self.forget_sign_contents()
            raise

        self.writes += 1
        self.last_write_at = self._now()
        self.last_error = None
        logger.debug("wrote %d bytes: %s", len(packet), packet.hex())

        if self._inter_packet_delay:
            await self._sleep(self._inter_packet_delay)

    async def _connect(self) -> None:
        was_open = self._transport.is_open
        await asyncio.to_thread(self._transport.ensure_open)
        if not was_open:
            self.forget_sign_contents()
            for hook in self._reconnect_hooks:
                try:
                    await hook()
                except Exception:
                    logger.exception("a reconnect hook failed")

    async def _reconnect_loop(self) -> None:
        """Keep trying to bring the link back while the service is running.

        This is the only thing that opens the link. A write used to open one
        lazily and no longer does, so if this task ever dies the service is
        down until it is restarted, however healthy the sign becomes. It
        therefore survives anything a single attempt can raise, not merely the
        failure the transport is expected to report. It died once on an
        arithmetic error from its own backoff.
        """
        while not self._stopping.is_set():
            if not self._transport.is_open:
                try:
                    await self._connect()
                except TransportError as err:
                    logger.debug("reconnect attempt failed: %s", err)
                except Exception:
                    logger.exception(
                        "reconnect attempt failed unexpectedly; still watching the link"
                    )

            delay = max(1.0, self._seconds_until_retry())
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except TimeoutError:
                continue

    def _seconds_until_retry(self) -> float:
        seconds_until_retry = getattr(self._transport, "seconds_until_retry", None)
        if seconds_until_retry is None:
            return 1.0
        return float(seconds_until_retry())
