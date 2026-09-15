"""Taking the whole sign over, and giving it back.

An alert is written to the sign's priority file, which by protocol suppresses
every other file until it is released. Releasing means a *bare* write to that
file, carrying the label and nothing else, at which point the sign resumes its
run sequence by itself and nothing has to rebuild the rotation.

An ordinary write with an empty body is not a release and was measured not to
be: it still carries a Start-of-Message byte, a position byte and a mode, which the
sign reads as a blank priority message and displays, keeping the screen rather
than handing it back. See :meth:`SignController.clear_priority`.

The release deadline is persisted. A service that restarted during an alert and
forgot about it would leave the sign stuck showing that alert forever, with the
rotation invisible behind it and no record of why.

An alert can call variables and icons, rendered through the registry's
:meth:`SlotRegistry.rendering`, which holds the registry's lock until the
priority file is written. Its lock is always taken before this service's own.

Only :meth:`raise_alert` hands that method a message. The three paths that put
an alert back on the sign, :meth:`restore`, :meth:`reassert` and
:meth:`lifted`, deliberately do not: an icon they name already has its picture
file, since the alert calling it is what keeps the file, and claiming is a thing
that can fail. Failing to put the sign back is a worse outcome than a call that
draws nothing.

The sign will not take a picture while a priority message is running. Measured
on 2026-09-13: a slot's icons, written under an alert and then revealed when the
alert was released, drew nothing or drew a sliver, and stayed that way until the
next refresh wrote them again. So a picture write has to have the sign to
itself, which is what :meth:`lifted` is for. The registry holds it around the
writes it makes, and :meth:`raise_alert` does the same for its own, since an
alert replacing an alert draws under the one still up.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from readerboard.protocol import constants as c
from readerboard.protocol.markup import render
from readerboard.protocol.tokens import MODE_BY_NAME
from readerboard.sign.controller import SignController
from readerboard.sign.state import AlertState, ServiceState, StateStore

logger = logging.getLogger(__name__)

class Renderer(Protocol):
    """What :meth:`SlotRegistry.rendering` hands back, and all this service uses of it.

    Two steps rather than one. Calling it renders; ``draw_icons`` puts the
    pictures its icons were given on the sign. Nothing here draws before it has
    decided the alert is one the priority file can hold, because those writes go
    into files the alert currently on the sign may be calling.
    """

    @property
    def draws_pictures(self) -> bool:
        """Whether :meth:`draw_icons` has anything to write."""
        ...

    async def revert(self) -> None:
        """Give back what was claimed, and redraw what it evicted."""
        ...

    def __call__(self, message: str, *, strict: bool = True) -> bytes:
        """Render a message to the bytes the sign is sent."""
        ...

    async def draw_icons(self) -> None:
        """Write the pictures claimed for this message. Once, after deciding."""
        ...


Rendering = Callable[[str | None], AbstractAsyncContextManager[Renderer]]


class _Lift:
    """What :meth:`AlertService.lifted` yields, for the caller to read afterwards.

    It carries one fact: whether an alert went back on the priority file as the
    hold closed. The caller needs it because putting the alert back is a real
    write that restarts the alert on the display, so re-asserting the alert
    after one has happened restarts it a second time for nothing. That second
    restart is the flicker the hand-back brought with it.

    It cannot be answered on the way in. The alert can be released or replaced
    while the sign is handed back, and one whose deadline passes in the meantime
    is dropped rather than shown again for a second, so the answer is only known
    once :meth:`AlertService._put_back` has run.
    """

    def __init__(self) -> None:
        """Start out having put nothing back, which is true until the hold closes."""
        self._put_back = False

    def record(self, put_back: bool) -> None:
        """Say whether the put-back wrote the priority file. Called as the hold closes."""
        self._put_back = put_back

    @property
    def put_the_alert_back(self) -> bool:
        """Whether an alert was written to the priority file as the hold closed."""
        return self._put_back


class AlertTooLong(ValueError):
    """The alert does not fit the sign's fixed size priority file."""


class AlertAlreadyActive(Exception):
    """An alert is holding the sign and the caller asked not to replace one.

    The state of the sign rather than anything wrong with the request, which is
    what makes it a conflict and not a bad request. Raising an alert replaces
    whatever is on the priority file, which is what a caller wants when the new
    alert is the more important one and is exactly what a caller does not want
    when two sources raise alerts independently and neither knows about the
    other.
    """


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _PlainRenderer:
    """The renderer for a service with no registry: no variables, no icons."""

    @property
    def draws_pictures(self) -> bool:
        """Nothing was claimed, so nothing is going to be drawn."""
        return False

    async def revert(self) -> None:
        """Nothing was claimed, so there is nothing to give back."""

    def __call__(self, message: str, *, strict: bool = True) -> bytes:
        """Render against nothing, so a call to either is refused or draws nothing."""
        return render(message, strict=strict)

    async def draw_icons(self) -> None:
        """Nothing was claimed, so there is nothing to draw."""


@contextlib.asynccontextmanager
async def _without_variables(message: str | None = None) -> AsyncIterator[Renderer]:
    """Render with no variables or icons at all, for a service no registry is attached to.

    ``message`` is accepted and ignored. With no registry there is nothing that
    could give an icon a picture file, and a message calling one is refused by
    the renderer itself.
    """
    yield _PlainRenderer()


class AlertService:
    """Owns the priority file and the deadline for giving it back."""

    def __init__(
        self,
        controller: SignController,
        store: StateStore,
        state: ServiceState,
        *,
        now: Callable[[], datetime] = _utcnow,
        rendering: Rendering = _without_variables,
    ) -> None:
        """Wire the alert service to the sign and to its restored state.

        ``rendering`` is where an alert is rendered, and is the registry's
        :meth:`SlotRegistry.rendering` in the service. Without one an alert
        cannot call a variable, and one that tries is refused.
        """
        self._controller = controller
        self._store = store
        self._state = state
        self._now = now
        self._rendering = rendering
        self._lock = asyncio.Lock()

    def set_rendering(self, rendering: Rendering) -> None:
        """Render alerts through the registry, so that they can call variables."""
        self._rendering = rendering

    @property
    def active(self) -> AlertState | None:
        """The alert currently holding the sign, if any."""
        return self._state.alert

    async def restore(self) -> None:
        """Re-establish or clean up an alert that was running before a restart."""
        alert = self._state.alert
        if alert is None:
            # The sign may still be holding an alert from before an unclean
            # stop, and there is no way to ask it. Releasing costs one small
            # write and guarantees the rotation is visible.
            await self._controller.clear_priority()
            return

        if not alert.message:
            # An earlier version accepted an empty alert, and writing one is the
            # protocol's own release sequence: the sign hands itself back while
            # the state file goes on calling the alert active, and every restart
            # and re-assert repeats it. There is nothing here the sign can hold,
            # so let it go and record that it is gone.
            logger.warning(
                "an alert with no message was recorded before the restart; releasing it"
            )
            await self.release()
            return

        if alert.expires_at is not None and alert.expires_at <= self._now():
            logger.info("an alert was active before the restart but has since expired")
            await self.release()
            return

        async with self._rendering(None) as render_message:
            body = render_message(alert.message, strict=False)
            fits = len(body) <= c.PRIORITY_FILE_CAPACITY
            if fits:
                # What is logged depends on whether anything went out, because
                # by here it often has. The registry hands the sign back to
                # write a picture and puts the alert straight back on, and its
                # restore runs before this one, so on a sign with icons the
                # alert is already up and this write is suppressed. Saying
                # "restoring" over no packet at all is how a log stops being
                # worth reading.
                if await self._write(alert, body):
                    logger.info("restored the alert that was active before the restart")
                else:
                    logger.info(
                        "the alert that was active before the restart is already back "
                        "on the sign, put back after the pictures were written"
                    )

        if not fits:
            # It fitted when it was accepted, and it does not now. Re-rendering
            # a stored alert is deliberately lenient, so a markup token this
            # version no longer knows comes back as its own literal text, which
            # is longer than the two or three bytes it used to render to. Enough
            # of those and the alert outgrows the priority file.
            #
            # Letting that raise would be worse than it sounds. The exception is
            # a ValueError rather than a TransportError, so it would escape the
            # startup path that tolerates an unreachable sign, and the service
            # would refuse to start at all until somebody edited the state file
            # by hand. The alert is not recoverable either way, so say so and
            # give the sign back.
            logger.warning(
                "the stored alert no longer fits the sign's priority file, which holds "
                "%d bytes; releasing it. It probably used a markup token this version "
                "has removed. The text was: %r",
                c.PRIORITY_FILE_CAPACITY,
                alert.message,
            )
            await self.release()

    async def reassert(self) -> bool:
        """Write the active alert to the sign again, if there is one.

        The registry's periodic refresh exists because the sign can be power
        cycled behind a still-connected adapter, leaving it blank with nothing
        to notice. That refresh puts the slots back, and an alert lives in the
        priority file, which it touches only when it hands the sign over to
        write a picture. Without this, a sign power cycled mid-alert with no
        picture to write would sit blank until the alert's deadline passed, and
        an alert with no deadline would sit blank indefinitely.

        So the caller asks first. :meth:`SlotRegistry.refresh` and
        :meth:`SlotRegistry.reboot` each report whether they put the alert back,
        and this is for the case where they did not. Calling it anyway is not
        harmless: the write is forced, so it would restart an alert already back
        on the display, once per refresh interval for as long as the alert is up.

        Returns whether there was an alert to re-assert.
        """
        async with self._rendering(None) as render_message, self._lock:
            alert = self._state.alert
            if alert is None:
                return False
            if not alert.message:
                # The same rule :meth:`restore` applies, and here because an
                # empty alert can outlive that: a sign unreachable at startup
                # makes restore raise before it reaches its own branch, and the
                # record survives into the first refresh. Writing it is the
                # opposite of a release, since the formatting bytes around the
                # empty body make the sign hold the display dark. There is
                # nothing here the sign can hold, so let it go and record that.
                logger.warning(
                    "an alert with no message is recorded as active; releasing it"
                )
                await self._release_locked()
                return False
            body = render_message(alert.message, strict=False)
            if len(body) > c.PRIORITY_FILE_CAPACITY:
                # See restore() for how a stored alert outgrows the file. This
                # runs on a timer and on every reconnect, so raising here would
                # be a warning every fifteen minutes for something nobody can
                # act on from a log line.
                logger.warning(
                    "the active alert no longer fits the sign's priority file; not "
                    "re-asserting it. The text was: %r",
                    alert.message,
                )
                return False
            # Forced, because what is in doubt here is precisely whether the
            # sign still holds what the controller believes it does. Left to
            # suppression this would write nothing at all.
            await self._write(alert, body, force=True)

        return True

    @contextlib.asynccontextmanager
    async def lifted(self, render: Callable[..., bytes]) -> AsyncIterator[_Lift]:
        """Take the alert off the sign for the duration, then put it straight back.

        For a picture write, which the sign will not take while a priority
        message is running. The module docstring has what was measured; the
        short version is that the write is lost and nothing retries it, because
        the controller believes it already sent those bytes.

        ``render`` is the registry's own renderer, handed in rather than reached
        for. This runs with the registry's lock held, and every renderer this
        service has takes that lock, so asking for one here would wait on a lock
        the caller is holding.

        The sign is handed back whether or not an alert is recorded, and that
        is the point rather than an oversight. A state file is not a reading of
        the sign: an unclean stop between the write and the save leaves a
        takeover nothing here knows about, which is why :meth:`restore` clears
        the priority file at startup even with no alert recorded. It does that
        after the registry has restored, though, so a picture written on the way
        back up would still go out underneath it. Releasing here makes "a
        picture write has the sign to itself" true of the sign rather than true
        of the record.

        It is not a write per picture. The release is unforced, so the
        controller sends it once and suppresses every repeat until something
        puts a message back on that file.

        The put-back is forced and happens either way: a write that failed
        under a lifted alert must not leave the sign showing the rotation with
        an alert recorded as holding it.

        What differs is what happens when the put-back *itself* fails, and the
        two exits want opposite things. If the body raised, that failure is the
        one worth reporting and this one is logged and swallowed, or a caller
        whose pool was full would be told the sign is unreachable. If the body
        succeeded, nothing is in flight to protect and the sign is the thing
        left wrong, so it travels: answering 200 over a display that is showing
        the rotation while the service still reports an alert would be the call
        lying about what it did.

        What is yielded reports, afterwards, whether an alert went back on the
        priority file, so that the caller does not write it a second time. See
        :class:`_Lift`.
        """
        lift = _Lift()
        async with self._lock:
            # Unconditional, and unforced. See the docstring: what is recorded
            # here is not what the sign is holding, and the release costs one
            # write per process because the controller remembers it.
            await self._controller.clear_priority()

            # Read under the lock, after the release. Nothing can have taken the
            # sign over in between, so an alert found here is one to put back.
            alert = self._state.alert
            if alert is None:
                yield lift
                return

            try:
                yield lift
            except BaseException:
                lift.record(await self._put_back_quietly(alert, render))
                raise
            else:
                lift.record(await self._put_back(alert, render))

    async def _put_back_quietly(
        self, alert: AlertState, render: Callable[..., bytes]
    ) -> bool:
        """Put a lifted alert back while another failure is already travelling.

        Never raises. Every caller is on a rollback path, so anything raised
        here would arrive in place of the failure that brought us to it, and a
        caller told the sign is unreachable when its pool was actually full has
        been told the wrong thing about its own request. :meth:`reassert` puts
        the alert back on its own timer, so the sign is repaired either way.

        The caller reverts the pool before calling this, so the alert is
        rendered against the files it had when it was up rather than against the
        ones the alert that failed had taken off it.

        Returns whether the priority file was written, on the same terms as
        :meth:`_put_back`. A failure swallowed here wrote nothing, so it answers
        False and whatever asked is told to put the alert back itself.
        """
        try:
            return await self._put_back(alert, render)
        except Exception:
            # Anything at all, for the reason above. Not BaseException: a
            # cancellation is the caller going away and has to keep travelling.
            logger.exception("could not put the lifted alert back on the sign")
            return False

    async def _put_back(self, alert: AlertState, render: Callable[..., bytes]) -> bool:
        """Write an alert that was lifted off the sign back onto the priority file.

        Returns whether the priority file was written, which is what tells the
        caller it has nothing left to do. Every early return below answers
        False, so an alert this declined to put back is still re-asserted by
        whoever asked, rather than being dropped by both of them.

        Raises what the write raised, which is what the success path wants: the
        sign has been handed back, this is what puts it right, and a call that
        reported success over a failure here would leave the rotation showing
        with an alert still recorded as holding the display and nothing to say
        so until the periodic re-assert. The rollback paths want the opposite
        and go through :meth:`_put_back_quietly`.

        The four early returns are decisions rather than failures: the alert is
        not this one any more, it has expired, it has no message, or it no
        longer fits.
        """
        if self._state.alert is not alert:
            # Released or replaced while it was lifted. Whoever did that owns
            # the priority file now, and putting this back would undo them.
            return False

        if not alert.message:
            # An earlier version accepted an alert with no text, and one can
            # still be sitting in a state file written by it. Writing it is the
            # opposite of putting the sign back: the body is empty but the
            # formatting bytes around it are not, so the sign reads a blank
            # priority message and holds the display dark until something
            # releases it. The lifted hold has already sent that release, so
            # forget the stale record here as well.
            logger.warning(
                "the lifted alert has no message, so it was not put back; it will be "
                "released rather than restored"
            )
            self._state.alert = None
            self._store.save(self._state)
            return False

        if self._has_expired(alert):
            # Its deadline passed while the sign was being written to. The sweep
            # is about to release it, so putting it back would show it once more
            # for a second or two and then take it away again.
            logger.info("the lifted alert had expired, so it was not put back")
            self._state.alert = None
            self._store.save(self._state)
            return False

        body = render(alert.message, strict=False)
        if len(body) > c.PRIORITY_FILE_CAPACITY:
            # See restore() for how a stored alert outgrows the file. Not a
            # failure to report: there is nothing the caller could do about it
            # and nothing it did to cause it.
            logger.warning(
                "the lifted alert no longer fits the sign's priority file, so it "
                "was not put back. The text was: %r",
                alert.message,
            )
            return False
        # Forced, because the release above left the controller believing the
        # priority file holds a release. Without it this would be suppressed as
        # a repeat of the write before the lift.
        await self._write(alert, body, force=True)
        return True

    async def raise_alert(
        self,
        message: str,
        *,
        mode: str,
        ttl_seconds: float | None = None,
        fail_if_active: bool = False,
    ) -> AlertState:
        """Take the sign over with an alert.

        ``fail_if_active`` refuses rather than replaces when the sign is
        already held. An alert whose deadline has passed does not count as
        holding it: the sweep that releases one runs on a timer, so an alert
        that expired a moment ago is still recorded, and refusing for something
        nobody wanted kept would make the answer depend on where in that second
        the call arrived.
        """
        # The render, the write and recording the alert all happen inside the
        # rendering. That is what lets the registry refuse to delete a variable
        # the alert calls: it reads the recorded alert under the same lock, so
        # it never sees one that is rendered but not yet recorded.
        #
        # Persisting it does not. The rendering is a transaction: anything that
        # escapes it takes the alert's newly claimed pictures back, on the
        # reading that an alert which did not land calls nothing. Once the
        # priority file is written that reading is wrong, and a save that failed
        # inside it would hand away the files the alert on the sign is drawing
        # from. So the save happens after, where a failure costs the record on
        # disk and nothing on the sign.
        async with self._rendering(message) as render_message, self._lock:
            # First, so that a refusal renders nothing, writes nothing and takes
            # nothing from the picture pool. The rendering undoes any claim made
            # inside it, which is what makes raising this here safe rather than
            # merely early.
            current = self._state.alert
            if fail_if_active and current is not None and not self._has_expired(current):
                raise AlertAlreadyActive(
                    "an alert has been holding the sign since %s and this call asked to "
                    "be refused rather than replace one. Release that alert first, or "
                    "send this again without fail_if_active."
                    % current.started_at.isoformat()
                )

            body = render_message(message)
            if len(body) > c.PRIORITY_FILE_CAPACITY:
                raise AlertTooLong(
                    "the alert renders to %d bytes but the sign's priority file holds %d "
                    "and cannot be resized" % (len(body), c.PRIORITY_FILE_CAPACITY)
                )

            now = self._now()
            alert = AlertState(
                message=message,
                mode=mode,
                started_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
            )

            # Only now. Up to here nothing has been written, so the refusal
            # above leaves the sign holding whatever it held, icons included;
            # drawing before it would have put this alert's pictures into files
            # the alert already showing is calling.
            # The alert this one is replacing, when replacing it means handing
            # the sign back first. An alert is up and this one brings a picture,
            # which the sign will not take while a priority message is running.
            # The release is the alert service's own to make rather than the
            # registry's, because the registry is called from inside this lock.
            # Gated on there being a picture at all: without one, every alert
            # replacing an alert would hand the sign back for nothing.
            replaced = current if render_message.draws_pictures else None
            if render_message.draws_pictures:
                # Gated on there being a picture and on nothing else. Whether an
                # alert is recorded says nothing about whether the sign is
                # holding one: an unclean stop leaves a takeover this service
                # never wrote down, and the picture below would be lost under it
                # exactly as it would under an alert it does know about.
                # Unforced, so it is suppressed when the file is already free.
                await self._controller.clear_priority()

            try:
                await render_message.draw_icons()
                await self._write(alert, body)
            except BaseException:
                if replaced is not None:
                    # The sign was handed back for the picture and this alert
                    # never made it onto the file, so the display is showing the
                    # rotation with an alert still recorded as holding it. Put
                    # the old one back. Without this the hand-back would be a
                    # regression on its own: before it, a write that failed here
                    # left the alert that was up still up, because nothing had
                    # touched the priority file.
                    #
                    # Giving back what this alert claimed comes first, and with
                    # a full picture pool it is what decides whether the old
                    # alert comes back whole. Claiming this one's icon can evict
                    # one only the alert being replaced calls, and the registry
                    # does not undo that until this failure reaches it, so
                    # putting the alert back before this renders it without the
                    # icon it is still asking for.
                    try:
                        await render_message.revert()
                    except Exception:
                        # A redraw is a write, and the thing that brought us here
                        # is usually a sign that stopped answering. The refresh
                        # repairs the pool; what must not happen is this arriving
                        # in place of the failure the caller asked about.
                        logger.exception(
                            "could not give back what the refused alert claimed"
                        )

                    # Quietly, because the failure being handled is the one the
                    # caller asked about and this one is not.
                    await self._put_back_quietly(replaced, render_message)
                raise

            self._state.alert = alert

        self._store.save(self._state)

        logger.info(
            "alert raised%s",
            " until %s" % alert.expires_at.isoformat() if alert.expires_at else " with no deadline",
        )
        return alert

    async def release(self) -> bool:
        """Give the sign back. Returns whether an alert was actually holding it."""
        async with self._lock:
            was_active = await self._release_locked()

        if was_active:
            logger.info("alert released, rotation resumes")

        return was_active

    async def _release_locked(self) -> bool:
        """Give the sign back with the lock already held. Returns whether one was up.

        Split out for :meth:`reassert`, which holds the lock across its whole
        body and cannot call :meth:`release`, because ``asyncio.Lock`` is not
        reentrant and waiting on it there would wait forever.
        """
        was_active = self._state.alert is not None
        await self._controller.clear_priority()
        self._state.alert = None
        self._store.save(self._state)
        return was_active

    async def sweep(self) -> bool:
        """Release the alert if its deadline has passed. Returns whether it did."""
        alert = self._state.alert
        if alert is None or not self._has_expired(alert):
            return False

        logger.info("alert reached its deadline")
        await self.release()
        return True

    def _has_expired(self, alert: AlertState) -> bool:
        """Whether an alert's deadline has passed, on the service's own clock."""
        return alert.expires_at is not None and alert.expires_at <= self._now()

    async def _write(self, alert: AlertState, body: bytes, *, force: bool = False) -> bool:
        """Put an alert on the priority file. Returns False if the write was suppressed."""
        return await self._controller.write_priority(
            body, mode=MODE_BY_NAME[alert.mode].value, force=force
        )
