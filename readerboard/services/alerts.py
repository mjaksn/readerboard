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

Two paths hand that method a message, and so claim picture files: showing an
alert raised by a caller, and :meth:`AlertService.promote` showing one that
waited its turn. Checking an alert before it joins the queue does not claim:
it claims when it takes the sign. The three paths that put an alert back on the
sign, :meth:`restore`, :meth:`reassert` and :meth:`lifted`, deliberately do
not either: an icon they name already has its picture file, since the alert
calling it is what keeps the file, and claiming is a thing that can fail.
Failing to put the sign back is a worse outcome than a call that draws nothing.

An alert can also wait its turn. One raised with ``if_active="queue"`` while
the sign is held joins a queue, kept in the state file beside the alert, and
:meth:`AlertService.promote` puts the head of it on the sign once nothing holds
it. Promotion runs from the sweep loop rather than wherever an alert ends,
because an alert ends in several places and one of them,
:meth:`AlertService._put_back`, runs with the registry's lock already held,
while rendering the next alert has to take that lock before this service's own.
The sweep holds neither, so one step there covers every way an alert can end.
An alert that reaches its ttl is followed in the same sweep that releases it;
one that ends anywhere else without a caller asking, such as one dropped on its
way back from a picture write because its deadline passed meanwhile, leaves up
to a sweep interval of the rotation showing first.
:meth:`AlertService.dismiss`, a caller releasing the alert by hand, holds no
lock either and promotes before it returns, so that a second release sent
straight after reaches the alert that followed rather than a gap.

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
from typing import Literal, Protocol

from readerboard.protocol import constants as c
from readerboard.protocol.markup import render
from readerboard.protocol.tokens import MODE_BY_NAME
from readerboard.services.registry import RegistryError
from readerboard.sign.controller import SignController
from readerboard.sign.state import AlertState, QueuedAlertState, ServiceState, StateStore
from readerboard.transport.base import TransportError

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

    def check(self, message: str) -> bytes:
        """Render a message strictly as it would once its icons had files, claiming none."""
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
    """An alert is holding the sign or waiting for it, and the caller asked to be refused.

    The state of the sign rather than anything wrong with the request, which is
    what makes it a conflict and not a bad request. Raising an alert replaces
    whatever is on the priority file, which is what a caller wants when the new
    alert is the more important one and is exactly what a caller does not want
    when two sources raise alerts independently and neither knows about the
    other.
    """


class AlertQueueFull(Exception):
    """As many alerts are waiting as ``alert_queue_limit`` allows."""


class AlertNotWaiting(Exception):
    """No alert with that key is waiting its turn."""


class _SignTaken(Exception):
    """The sign was held by the time an alert that would only wait got the locks.

    Raised inside the registry's rendering, so that whatever it claimed for the
    alert is given back on the way out, and caught by the caller, which queues
    the alert instead or leaves the queue for the next sweep.
    """


class _HeadMoved(Exception):
    """The alert being promoted was no longer at the head once the locks were held.

    Dropped for having waited too long, cancelled, or updated by its key in the
    meantime. Raised inside the rendering for the same reason as
    :class:`_SignTaken`, and caught by :meth:`AlertService.promote`, which
    looks at the queue again.
    """


IfActive = Literal["replace", "queue", "fail"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _too_long(size: int) -> str:
    return (
        "the alert renders to %d bytes but the sign's priority file holds %d and cannot "
        "be resized" % (size, c.PRIORITY_FILE_CAPACITY)
    )


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

    def check(self, message: str) -> bytes:
        """Render strictly against nothing, which refuses a call to either."""
        return render(message, strict=True)

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
        queue_limit: int = 10,
    ) -> None:
        """Wire the alert service to the sign and to its restored state.

        ``rendering`` is where an alert is rendered, and is the registry's
        :meth:`SlotRegistry.rendering` in the service. Without one an alert
        cannot call a variable, and one that tries is refused.

        ``queue_limit`` is how many alerts may wait their turn at once, which is
        the ``alert_queue_limit`` setting in the service.
        """
        self._controller = controller
        self._queue_limit = queue_limit
        self._store = store
        self._state = state
        self._now = now
        self._rendering = rendering
        self._lock = asyncio.Lock()
        # Set before anything that changes the priority file, and cleared only
        # once the sign and the record agree again. So any way of stopping short
        # leaves it set: a failure, a failure swallowed on a rollback, or a
        # cancellation. See :attr:`sign_may_not_match`.
        self._sign_may_not_match = False

    @property
    def sign_may_not_match(self) -> bool:
        """Whether the sign's priority file may not match the recorded alert.

        The refresh asks the sign whether it still holds a picture, and leaves
        the alert alone when it does, because the priority file goes with the
        rest of the sign's memory. That says nothing about a change this
        service made to the priority file itself and did not finish: an alert
        lifted for a picture and not put back, whether the put-back failed, was
        swallowed by :meth:`_put_back_quietly` or was cancelled, or a write or
        release cancelled after it reached the sign and before the record caught
        up. So the refresh's caller re-asserts when this is set, whatever the
        sign answered, and the re-assert makes the sign match the record.
        """
        return self._sign_may_not_match

    def set_rendering(self, rendering: Rendering) -> None:
        """Render alerts through the registry, so that they can call variables."""
        self._rendering = rendering

    @property
    def active(self) -> AlertState | None:
        """The alert currently holding the sign, if any."""
        return self._state.alert

    @property
    def waiting(self) -> list[QueuedAlertState]:
        """The alerts waiting their turn, the next one first. A copy."""
        return list(self._state.alert_queue)

    def position(self, waiting: QueuedAlertState) -> int | None:
        """Where a waiting alert stands, 1 for the next to take the sign, or None if it is not."""
        for index, candidate in enumerate(self._state.alert_queue):
            if candidate is waiting:
                return index + 1
        return None

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

        So the caller asks first. :meth:`SlotRegistry.refresh` reports whether
        the alert can be left alone, which it can when the refresh put it back
        or the sign answered that it kept its memory, and
        :meth:`SlotRegistry.reboot` whether it put the alert back; this is for
        the case where neither holds, or :attr:`sign_may_not_match` says a
        change to the priority file was not finished. Calling it anyway is not
        harmless: the write is forced, so it would restart an alert the sign
        already holds, once per refresh interval for as long as the alert is up.

        Returns whether there was an alert to re-assert.
        """
        async with self._rendering(None) as render_message, self._lock:
            alert = self._state.alert
            if alert is None:
                if self._sign_may_not_match:
                    # Nothing is recorded, and something may be on the file: a
                    # write or a release stopped short. Forced, because the
                    # controller's record of the file is in doubt too.
                    await self._controller.clear_priority(force=True)
                    self._sign_may_not_match = False
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
                # act on from a log line. Released rather than left recorded,
                # as restore() does, because a record nothing can put on the
                # sign would hold every waiting alert behind it for good.
                logger.warning(
                    "the active alert no longer fits the sign's priority file; "
                    "releasing it. The text was: %r",
                    alert.message,
                )
                await self._release_locked()
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
            # Off the sign from the release on, until a put-back lands, which is
            # what clears it. Set before the release rather than after it: a
            # cancellation while the release is under way can still let it reach
            # the sign, since the write runs in a worker thread, and must not
            # leave the alert off with nothing to say so. See
            # :attr:`sign_may_not_match`.
            #
            # The release is forced if the flag was already set, because the
            # controller's record of the file may then be wrong: an earlier
            # write that reached the sign without being recorded would leave it
            # believing the file is free, and suppress the release the picture
            # below depends on.
            force = self._sign_may_not_match
            if self._state.alert is not None:
                self._sign_may_not_match = True

            # Unconditional, and unforced unless in doubt. See the docstring:
            # what is recorded here is not what the sign is holding, and the
            # release costs one write per process because the controller
            # remembers it.
            await self._controller.clear_priority(force=force)

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
        been told the wrong thing about its own request. A failure here leaves
        :attr:`sign_may_not_match` set, and the next refresh re-asserts the
        alert because of it, so the sign is repaired either way.

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
        so: the refresh leaves the alert alone on a sign that answers it kept
        its memory. The rollback paths want the opposite and go through
        :meth:`_put_back_quietly`, which records the failure instead.

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
        if_active: IfActive = "replace",
        key: str | None = None,
        max_wait_seconds: float | None = None,
    ) -> AlertState | QueuedAlertState:
        """Take the sign over with an alert, or queue it behind the one holding it.

        ``if_active`` says what happens when the sign is already held: replace
        the alert holding it, wait for it to end, or be refused. With ``queue``
        or ``fail``, an alert waiting its turn counts as holding the sign too,
        so a call made in the moment between one alert ending and the next
        being promoted neither jumps the queue nor slips past a refusal. With
        ``replace`` the waiting alerts are left as they are.

        An alert whose deadline has passed does not hold the sign: the sweep
        that releases one runs on a timer, so an alert that expired a moment ago
        is still recorded, and treating it as holding the sign would make the
        answer depend on where in that second the call arrived.

        Returns the alert holding the sign, or the waiting alert if it was
        queued.
        """
        while True:
            if if_active == "queue":
                # Checked first, against the files that already exist, so that an
                # alert that is only going to wait claims no picture file and
                # evicts nothing: an eviction undone is a picture redrawn, and a
                # picture write blanks the display.
                async with self._rendering(None) as renderer, self._lock:
                    if self._sign_is_held():
                        return self._enqueue(
                            renderer,
                            message,
                            mode=mode,
                            ttl_seconds=ttl_seconds,
                            key=key,
                            max_wait_seconds=max_wait_seconds,
                        )
            try:
                return await self._show(
                    message, mode=mode, ttl_seconds=ttl_seconds, if_active=if_active, key=key
                )
            except _SignTaken:
                # Something took the sign between the check above and the locks
                # below. Go round again, and queue it this time.
                continue

    async def _show(
        self,
        message: str,
        *,
        mode: str,
        ttl_seconds: float | None,
        if_active: IfActive,
        key: str | None,
    ) -> AlertState:
        """Put an alert on the sign now, unless ``if_active`` says the sign being held stops it."""
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
            # inside it, which is what makes raising either of these here safe
            # rather than merely early.
            if if_active != "replace" and self._sign_is_held():
                if if_active == "queue":
                    raise _SignTaken
                raise AlertAlreadyActive(self._why_fail_was_refused())

            body = render_message(message)
            if len(body) > c.PRIORITY_FILE_CAPACITY:
                raise AlertTooLong(_too_long(len(body)))

            now = self._now()
            alert = AlertState(
                message=message,
                mode=mode,
                started_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
                key=key,
            )
            await self._take_the_sign(render_message, alert, body)

        self._store.save(self._state)

        logger.info(
            "alert raised%s",
            " until %s" % alert.expires_at.isoformat() if alert.expires_at else " with no deadline",
        )
        return alert

    def _why_fail_was_refused(self) -> str:
        """Say what is in the way of an alert sent with if_active fail, and how to clear it.

        Releasing the alert showing only clears the way when nothing is waiting
        behind it, since the next waiting one goes straight up; and with nothing
        showing, only the queue is in the way.
        """
        current = self._state.alert
        waiting = len(self._state.alert_queue)
        if current is not None and not self._has_expired(current):
            if not waiting:
                return (
                    "an alert has been holding the sign since %s and this call asked to be "
                    "refused rather than replace one. Release it with DELETE /alerts, send "
                    "this again with if_active replace to replace it, or with if_active "
                    "queue to wait for it." % current.started_at.isoformat()
                )
            return (
                "an alert has been holding the sign since %s with %d more waiting behind "
                "it, and this call asked to be refused rather than replace one. Releasing "
                "it puts the next one up; DELETE /alerts with clear_queue true releases it "
                "and cancels the waiting ones. Or send this again with if_active replace "
                "to replace it, or with if_active queue to wait behind them."
                % (current.started_at.isoformat(), waiting)
            )
        return (
            "%d alert(s) are waiting for the sign and this call asked to be refused rather "
            "than wait. DELETE /alerts/queue cancels them. Or send this again with "
            "if_active replace to go up now, or with if_active queue to wait behind them."
            % waiting
        )

    async def _take_the_sign(self, render_message: Renderer, alert: AlertState, body: bytes) -> None:
        """Draw an accepted alert's icons, write it and record it. Both locks are held.

        Shared by an alert raised by a caller and one promoted from the queue.
        Nothing here refuses anything; the alert has been accepted by the time
        it gets here.
        """
        current = self._state.alert
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
            #
            # An alert being replaced is off the sign from here until a
            # write lands, this one's or the old one's put back, exactly as
            # under :meth:`lifted`. Set before the release for the same
            # reason, and cleared only by that write. Forced if it was
            # already set, for the reason given in :meth:`lifted`.
            force = self._sign_may_not_match
            if replaced is not None:
                self._sign_may_not_match = True
            await self._controller.clear_priority(force=force)

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
                    logger.exception("could not give back what the refused alert claimed")

                # Quietly, because the failure being handled is the one the
                # caller asked about and this one is not.
                await self._put_back_quietly(replaced, render_message)
            raise

        self._state.alert = alert

    def _sign_is_held(self) -> bool:
        """Whether an alert holds the sign or is waiting for it. Under the lock.

        A waiting alert counts, so that nothing slips in ahead of the queue in
        the moment between one alert ending and the sweep promoting the next.
        """
        current = self._state.alert
        return (current is not None and not self._has_expired(current)) or bool(
            self._state.alert_queue
        )

    def _enqueue(
        self,
        renderer: Renderer,
        message: str,
        *,
        mode: str,
        ttl_seconds: float | None,
        key: str | None,
        max_wait_seconds: float | None,
    ) -> QueuedAlertState:
        """Add an alert to the queue, or update the waiting one with its key. Under both locks."""
        body = renderer.check(message)
        if len(body) > c.PRIORITY_FILE_CAPACITY:
            raise AlertTooLong(_too_long(len(body)))

        now = self._now()
        waiting = QueuedAlertState(
            message=message,
            mode=mode,
            queued_at=now,
            ttl_seconds=ttl_seconds,
            wait_until=now + timedelta(seconds=max_wait_seconds) if max_wait_seconds else None,
            key=key,
        )
        queue = self._state.alert_queue

        if key is not None:
            for index, candidate in enumerate(queue):
                if candidate.key == key:
                    # Updated where it stands rather than sent to the back, so
                    # that a caller sending the same alert again until it is
                    # seen does not lose its place by doing so. Only a waiting
                    # alert is matched: one holding the sign has been seen, and
                    # this joins the back behind it like any other.
                    waiting.queued_at = candidate.queued_at
                    queue[index] = waiting
                    self._store.save(self._state)
                    logger.info("updated the waiting alert %r, %d in the queue", key, index + 1)
                    return waiting

        if len(queue) >= self._queue_limit:
            raise AlertQueueFull(
                "%d alerts are already waiting for the sign, which is as many as "
                "alert_queue_limit allows. Send this again once one has been shown, cancel "
                "one with DELETE /alerts/queue/{key}, or raise alert_queue_limit." % len(queue)
            )

        queue.append(waiting)
        self._store.save(self._state)
        logger.info(
            "alert queued%s, %d waiting",
            " as %r" % key if key is not None else "",
            len(queue),
        )
        return waiting

    async def promote(self) -> bool:
        """Put the next waiting alert on the sign if nothing holds it. Returns whether it did.

        Called from the sweep loop and from :meth:`dismiss`, neither of which
        holds a lock, so that this can take the registry's lock before its own
        as rendering always must. See the module docstring for why it is not
        called from every place an alert ends.

        A waiting alert that cannot be shown when its turn comes is dropped with
        a warning, and the one behind it is tried straight away: there is no
        caller left to tell, and holding it would hold every alert behind it.
        That is one that no longer fits or calls something that has gone, and
        one whose icons find every picture file held by icons the slots are
        calling at that moment. One that cannot be shown because the sign is
        unreachable stays at the head, and is not tried while the link is down,
        so a sign switched off does not log a failure every sweep.
        """
        # Bounded rather than open, although every round but the last removes
        # an alert or finds a different one at the head: a caller updating the
        # head by its key on every round would otherwise keep this going. The
        # queue can be longer than the limit, restored from a state file
        # written before the limit was lowered, and every alert in it gets its
        # round.
        rounds = max(self._queue_limit, len(self._state.alert_queue)) + 2
        for _ in range(rounds):
            outcome = await self._promote_head()
            if outcome != "again":
                return outcome == "shown"
        return False

    async def _promote_head(self) -> Literal["shown", "stopped", "again"]:
        """Try the alert at the head of the queue once.

        ``again`` means the head was dropped, or was no longer the head by the
        time the locks were held, and the queue is worth looking at again.
        """
        if self._state.alert is not None or not self._state.alert_queue:
            return "stopped"
        if not self._controller.is_connected:
            return "stopped"

        head = self._state.alert_queue[0]
        try:
            async with self._rendering(head.message) as render_message, self._lock:
                # Under the lock, and again here rather than only before it, so
                # that an alert whose limit on waiting passed while this waited
                # for the locks is dropped rather than shown.
                self._drop_waited_too_long()
                queue = self._state.alert_queue
                if self.active is not None:
                    raise _SignTaken
                if not queue or queue[0] is not head:
                    raise _HeadMoved

                body = render_message(head.message)
                if len(body) > c.PRIORITY_FILE_CAPACITY:
                    raise AlertTooLong(_too_long(len(body)))

                now = self._now()
                alert = AlertState(
                    message=head.message,
                    mode=head.mode,
                    started_at=now,
                    expires_at=now + timedelta(seconds=head.ttl_seconds)
                    if head.ttl_seconds
                    else None,
                    key=head.key,
                )
                await self._take_the_sign(render_message, alert, body)
                # By identity rather than by position. Nothing else changes the
                # queue while this holds the lock, so the head is still first;
                # removing it by what it is keeps that true of this line even if
                # something one day does.
                queue[:] = [waiting for waiting in queue if waiting is not head]
        except _SignTaken:
            return "stopped"
        except _HeadMoved:
            return "again"
        except (ValueError, RegistryError) as err:
            # Everything that refuses an alert raised by a caller is one of
            # these, and nothing about the sign or the link is.
            #
            # The refusal may have come before the locks were held, from
            # claiming pictures for the message as it stood when this looked.
            # By now that alert may have been updated by its key, cancelled,
            # or have waited past its limit, and none of those is this alert
            # failing to show: an updated one is tried again in the next round.
            # So the wait limit goes first, logging its own reason, and only an
            # alert still waiting is dropped and reported here.
            async with self._lock:
                self._drop_waited_too_long()
                queue = self._state.alert_queue
                if not any(waiting is head for waiting in queue):
                    return "again"
                queue[:] = [waiting for waiting in queue if waiting is not head]
                self._store.save(self._state)
            logger.warning(
                "dropped the waiting alert%s, which could not be shown when its turn came: "
                "%s. The text was: %r",
                " %r" % head.key if head.key is not None else "",
                # The refusal is a sentence of its own, and this one goes on.
                str(err).rstrip("."),
                head.message,
            )
            return "again"

        self._store.save(self._state)
        logger.info(
            "the next waiting alert took the sign%s, %d still waiting",
            " until %s" % alert.expires_at.isoformat() if alert.expires_at else "",
            len(self._state.alert_queue),
        )
        return "shown"

    def _drop_waited_too_long(self) -> None:
        """Drop every waiting alert whose limit on waiting has passed. Under the lock.

        Under it because :meth:`promote` writes the alert at the head with the
        lock held and only then takes it out of the queue. Run during that
        write, this would log the alert going up as dropped unseen and leave it
        out of a queue it was about to leave anyway, so the log would say an
        alert was never shown when it is on the sign.
        """
        now = self._now()
        queue = self._state.alert_queue
        kept = [w for w in queue if w.wait_until is None or w.wait_until > now]
        if len(kept) == len(queue):
            return
        for waiting in queue:
            if not any(waiting is k for k in kept):
                logger.info(
                    "a waiting alert%s waited since %s without taking the sign, and was "
                    "dropped unseen",
                    " %r" % waiting.key if waiting.key is not None else "",
                    waiting.queued_at.isoformat(),
                )
        queue[:] = kept
        self._store.save(self._state)

    async def cancel_waiting(self, key: str) -> None:
        """Take one waiting alert out of the queue by its key."""
        async with self._lock:
            queue = self._state.alert_queue
            for index, waiting in enumerate(queue):
                if waiting.key == key:
                    del queue[index]
                    self._store.save(self._state)
                    logger.info("cancelled the waiting alert %r", key)
                    return
        raise AlertNotWaiting("no alert with the key %r is waiting for the sign" % key)

    async def clear_waiting(self) -> int:
        """Empty the queue. Returns how many alerts were waiting."""
        async with self._lock:
            count = len(self._state.alert_queue)
            if count:
                self._state.alert_queue.clear()
                self._store.save(self._state)
                logger.info("cancelled %d waiting alert(s)", count)
            return count

    async def release(self, *, clear_queue: bool = False) -> bool:
        """Give the sign back. Returns whether an alert was actually holding it.

        The next waiting alert takes the sign at the following sweep, or before
        :meth:`dismiss` returns, unless ``clear_queue`` empties the queue in the
        same step, so that the rotation comes back and stays. Emptied only once
        the release has reached the sign: a release that fails leaves the alert
        recorded as holding it, and must not have thrown away the waiting ones
        on its way to failing.
        """
        async with self._lock:
            was_active = await self._release_locked()
            cleared = len(self._state.alert_queue) if clear_queue else 0
            if cleared:
                self._state.alert_queue.clear()
                self._store.save(self._state)

        if cleared:
            logger.info("cancelled %d waiting alert(s)", cleared)
        if was_active:
            self._log_released()

        return was_active

    def _log_released(self) -> None:
        logger.info(
            "alert released, %s",
            "the next waiting alert follows" if self._state.alert_queue else "rotation resumes",
        )

    async def dismiss(self, *, clear_queue: bool = False) -> bool:
        """Release the alert for a caller, and put the next waiting one up at once.

        Returns whether an alert was holding the sign. Left to the sweep, the
        next alert would follow up to a sweep interval later, and a second
        release sent in that gap would find nothing showing and dismiss nothing
        while being answered as though it had. :meth:`promote` goes past a
        waiting alert it has to drop, so a bad one at the head does not open
        that gap either.

        A promotion that cannot reach the sign is logged rather than raised. The
        release the caller asked for has happened by then, and the sweep tries
        the promotion again.
        """
        was_active = await self.release(clear_queue=clear_queue)
        try:
            await self.promote()
        except TransportError as err:
            logger.warning(
                "released the alert, and could not show the next waiting one yet: %s", err
            )
        return was_active

    async def _release_locked(self) -> bool:
        """Give the sign back with the lock already held. Returns whether one was up.

        Split out for :meth:`reassert`, which holds the lock across its whole
        body and cannot call :meth:`release`, because ``asyncio.Lock`` is not
        reentrant and waiting on it there would wait forever.
        """
        was_active = self._state.alert is not None
        # In doubt from before the release until the record says it happened.
        # Forced while already in doubt, because the controller's record of the
        # file may then be wrong and would suppress the release.
        force = self._sign_may_not_match
        self._sign_may_not_match = True
        await self._controller.clear_priority(force=force)
        self._state.alert = None
        self._store.save(self._state)
        self._sign_may_not_match = False
        return was_active

    async def sweep(self) -> bool:
        """Release the alert if its deadline has passed. Returns whether it did.

        Waiting alerts whose limit on waiting has passed are dropped here too,
        so that the queue reads true while an alert holds the sign for a long
        time, rather than only when the next one is promoted.

        Decided under the lock, so that the alert released is the one found to
        have expired. Read before taking it, an expired alert could be replaced
        while this waited, by an alert queued with nothing left to wait for,
        and the release would then take down the alert that had just been
        shown.
        """
        async with self._lock:
            self._drop_waited_too_long()
            alert = self._state.alert
            if alert is None or not self._has_expired(alert):
                return False
            logger.info("alert reached its deadline")
            await self._release_locked()

        self._log_released()
        return True

    def _has_expired(self, alert: AlertState) -> bool:
        """Whether an alert's deadline has passed, on the service's own clock."""
        return alert.expires_at is not None and alert.expires_at <= self._now()

    async def _write(self, alert: AlertState, body: bytes, *, force: bool = False) -> bool:
        """Put an alert on the priority file. Returns False if the write was suppressed."""
        # Forced while the sign may not match. The controller records what a
        # file holds only once a send has returned, so a release cancelled
        # partway can reach the sign and leave the old alert's bytes in the
        # cache, and the same alert raised again would then be suppressed and
        # clear the flag below with the sign still missing it.
        force = force or self._sign_may_not_match
        # And in doubt while this write is out. Every caller records the alert
        # with no await between this returning and the record changing, so a
        # cancellation can only land while the flag is set.
        self._sign_may_not_match = True
        written = await self._controller.write_priority(
            body, mode=MODE_BY_NAME[alert.mode].value, force=force
        )
        self._sign_may_not_match = False
        return written
