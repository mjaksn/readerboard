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

    def __call__(self, message: str, *, strict: bool = True) -> bytes:
        """Render a message to the bytes the sign is sent."""
        ...

    async def draw_icons(self) -> None:
        """Write the pictures claimed for this message. Once, after deciding."""
        ...


Rendering = Callable[[str | None], AbstractAsyncContextManager[Renderer]]


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
                        "on the sign, put there with the pictures it calls"
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
        to notice. That refresh puts the slots back, but an alert lives in the
        priority file, which the registry does not touch. Without this, a sign
        power cycled mid-alert would sit blank until the alert's deadline
        passed, and an alert with no deadline would sit blank indefinitely.

        Returns whether there was an alert to re-assert.
        """
        async with self._rendering(None) as render_message, self._lock:
            alert = self._state.alert
            if alert is None:
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
    async def lifted(self, render: Callable[..., bytes]) -> AsyncIterator[None]:
        """Take the alert off the sign for the duration, then put it straight back.

        For a picture write, which the sign will not take while a priority
        message is running. The module docstring has what was measured; the
        short version is that the write is lost and nothing retries it, because
        the controller believes it already sent those bytes.

        ``render`` is the registry's own renderer, handed in rather than reached
        for. This runs with the registry's lock held, and every renderer this
        service has takes that lock, so asking for one here would wait on a lock
        the caller is holding.

        Nothing happens at all when no alert is up, which is almost always, and
        that is what keeps an ordinary icon write as cheap as it was.

        The put-back is forced and happens whatever went wrong in between: a
        write that failed under a lifted alert must not leave the sign showing
        the rotation with an alert recorded as holding it. A put-back that
        itself fails is logged rather than raised, because it would replace the
        failure that brought us here, and :meth:`reassert` repairs exactly this
        state on its own timer.
        """
        alert = self._state.alert
        if alert is None:
            yield
            return

        async with self._lock:
            # Re-read under the lock. Something may have released the alert
            # between the check above and here, and putting back one that was
            # released would leave the sign held by an alert nothing records.
            alert = self._state.alert
            if alert is None:
                yield
                return

            await self._controller.clear_priority(force=True)
            try:
                yield
            finally:
                await self._put_back(alert, render)

    async def _put_back(self, alert: AlertState, render: Callable[..., bytes]) -> None:
        """Write a lifted alert to the priority file again. Never raises."""
        if self._state.alert is not alert:
            # Released or replaced while it was lifted. Whoever did that owns
            # the priority file now, and putting this back would undo them.
            return

        if self._has_expired(alert):
            # Its deadline passed while the sign was being written to. The sweep
            # is about to release it, so putting it back would show it once more
            # for a second or two and then take it away again.
            logger.info("the lifted alert had expired, so it was not put back")
            self._state.alert = None
            self._store.save(self._state)
            return

        try:
            body = render(alert.message, strict=False)
            if len(body) > c.PRIORITY_FILE_CAPACITY:
                # See restore() for how a stored alert outgrows the file.
                logger.warning(
                    "the lifted alert no longer fits the sign's priority file, so it "
                    "was not put back. The text was: %r",
                    alert.message,
                )
                return
            # Forced, because the release above left the controller believing the
            # priority file holds a release. Without it this would be suppressed
            # as a repeat of the write before the lift.
            await self._write(alert, body, force=True)
        except TransportError as err:
            # In a finally, during whatever failure caused it. Raising here would
            # replace that failure with this one, and the periodic re-assert puts
            # the alert back on its own.
            logger.warning("could not put the lifted alert back on the sign: %s", err)

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
            if current is not None and render_message.draws_pictures:
                # An alert is up and this one brings a picture, which the sign
                # will not take while a priority message is running. The release
                # is the alert's own to make, and the write below puts this
                # alert on the file a moment later, so the rotation shows for
                # that moment rather than the display going dark. Gated on there
                # being a picture at all: without one, every alert replacing an
                # alert would flash for nothing.
                await self._controller.clear_priority(force=True)
            await render_message.draw_icons()
            await self._write(alert, body)
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
            was_active = self._state.alert is not None
            await self._controller.clear_priority()
            self._state.alert = None
            self._store.save(self._state)

        if was_active:
            logger.info("alert released, rotation resumes")

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
