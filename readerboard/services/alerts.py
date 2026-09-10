"""Taking the whole sign over, and giving it back.

An alert is written to the sign's priority file, which by protocol suppresses
every other file until it is released. Releasing means a *bare* write to that
file, carrying the label and nothing else, at which point the sign resumes its
run sequence by itself and nothing has to rebuild the rotation.

An ordinary write with an empty body is not a release and was measured not to
be: it still carries a Start-of-Message byte, a position and a mode, which the
sign reads as a blank priority message and displays, keeping the screen rather
than handing it back. See :meth:`SignController.clear_priority`.

The release deadline is persisted. A service that restarted during an alert and
forgot about it would leave the sign stuck showing that alert forever, with the
rotation invisible behind it and no record of why.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from readerboard.protocol import constants as c
from readerboard.protocol.markup import render
from readerboard.protocol.tokens import MODE_BY_NAME, POSITION_BY_NAME
from readerboard.sign.controller import SignController
from readerboard.sign.state import AlertState, ServiceState, StateStore

logger = logging.getLogger(__name__)


class AlertTooLong(ValueError):
    """The alert does not fit the sign's fixed size priority file."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AlertService:
    """Owns the priority file and the deadline for giving it back."""

    def __init__(
        self,
        controller: SignController,
        store: StateStore,
        state: ServiceState,
        *,
        now: Callable[[], datetime] = _utcnow,
        on_release: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        """Wire the alert service to the sign and to its restored state.

        ``on_release`` runs after the sign has been handed back. The registry
        uses it to apply a run sequence it held back while the alert was up; see
        ``MessageRegistry._apply_run_sequence`` for why it holds one back.
        """
        self._controller = controller
        self._store = store
        self._state = state
        self._now = now
        self._on_release = on_release
        self._lock = asyncio.Lock()

    def set_release_hook(self, hook: Callable[[], Awaitable[object]]) -> None:
        """Attach something to run after the sign is handed back."""
        self._on_release = hook

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

        if not self._still_fits(alert):
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
            return

        logger.info("restoring the alert that was active before the restart")
        await self._write(alert)

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
        async with self._lock:
            alert = self._state.alert
            if alert is None:
                return False
            if not self._still_fits(alert):
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
            await self._write(alert, force=True)

        return True

    async def raise_alert(
        self,
        message: str,
        *,
        mode: str,
        position: str,
        ttl_seconds: float | None = None,
    ) -> AlertState:
        """Take the sign over with an alert."""
        body = render(message)
        if len(body) > c.PRIORITY_FILE_CAPACITY:
            raise AlertTooLong(
                "the alert renders to %d bytes but the sign's priority file holds %d "
                "and cannot be resized" % (len(body), c.PRIORITY_FILE_CAPACITY)
            )

        now = self._now()
        alert = AlertState(
            message=message,
            mode=mode,
            position=position,
            started_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
        )

        async with self._lock:
            await self._write(alert)
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

        if self._on_release is not None:
            await self._on_release()

        return was_active

    async def sweep(self) -> bool:
        """Release the alert if its deadline has passed. Returns whether it did."""
        alert = self._state.alert
        if alert is None or alert.expires_at is None:
            return False
        if alert.expires_at > self._now():
            return False

        logger.info("alert reached its deadline")
        await self.release()
        return True

    def _still_fits(self, alert: AlertState) -> bool:
        """Whether a stored alert still fits the priority file when re-rendered.

        Asked before writing back anything that was accepted by an earlier
        version, because the lenient re-render below can be longer than the
        strict one that was measured when the alert was raised.
        """
        return len(render(alert.message, strict=False)) <= c.PRIORITY_FILE_CAPACITY

    async def _write(self, alert: AlertState, *, force: bool = False) -> None:
        await self._controller.write_priority(
            render(alert.message, strict=False),
            mode=MODE_BY_NAME[alert.mode].value,
            position=POSITION_BY_NAME[alert.position].value,
            force=force,
        )
