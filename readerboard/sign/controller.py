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
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Wrap a transport. Nothing is sent until :meth:`start`.

        ``sleep`` is injected for the same reason ``now`` is: so a test can ask
        what the controller waited for rather than how long it actually took.
        Measuring elapsed wall time to check the inter-packet delay is a flaky
        thing to do, because a platform whose timer granularity is coarser than
        the delay can return from two of them in less than twice the delay, and
        the test then fails on a margin that says nothing about the code.
        """
        self._transport = transport
        self._inter_packet_delay = inter_packet_delay
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
        position: bytes = c.TEXT_POS_MIDDLE,
        force: bool = False,
    ) -> bool:
        """Put ``body`` in a sign file. Returns False if the write was suppressed.

        ``force`` writes even when the bytes match what the sign is believed to
        hold. It is for the case where that belief is exactly what is in doubt,
        such as re-asserting an alert after the sign may have been power cycled.
        """
        payload = frames.write_text_file(label, body, mode=mode, position=position)
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
        position: bytes = c.TEXT_POS_MIDDLE,
        force: bool = False,
    ) -> bool:
        """Take the display over with an alert, or release it if ``body`` is empty."""
        return await self.write_text_file(
            c.FILE_PRIORITY, body, mode=mode, position=position, force=force
        )

    async def clear_priority(self) -> bool:
        """Release a priority takeover so the run sequence resumes."""
        return await self.write_priority(b"")

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
        await self._send(frames.set_memory_config(allocations))
        # The sign is now empty, so nothing we thought we knew about it holds.
        self.forget_sign_contents()

    async def send_special(self, payload: bytes) -> None:
        """Send a special function such as a clock command.

        Never suppressed. Setting the clock to the same value it already holds
        is still worth doing, because the point is to correct drift we cannot
        see.
        """
        await self._send(payload)

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
        packet = frames.packet(payload)
        async with self._lock:
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
        """Keep trying to bring the link back while the service is running."""
        while not self._stopping.is_set():
            if not self._transport.is_open:
                try:
                    await self._connect()
                except TransportError as err:
                    logger.debug("reconnect attempt failed: %s", err)

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
