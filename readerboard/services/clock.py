r"""Keeping the sign's clock right.

The sign has its own battery-backed clock and it drifts. It also loses the time
when it loses power for long enough, and a sign showing ``<time>`` with a wrong
clock is worse than one showing nothing, because it looks fine.

This replaces the crontab line that used to do the job:

    1 * * * * time_str=`date --date="next minute" +"\%H\%M"` && curl ... SET_TIME

That line ran hourly whatever the state of the sign, and could do nothing about a
sign that came back up at ten past the hour. This service syncs on startup,
hourly, and on every reconnect, which is the closest signal available to "the
sign may just have been power cycled".

``now`` is injected rather than mocked, so the tests can be about what gets sent
at a given time rather than about patching the clock out from under the code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from readerboard.protocol import frames
from readerboard.sign.controller import SignController
from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def sign_day_of_week(moment: datetime) -> int:
    """Convert a datetime to the sign's day numbering, 1 for Sunday to 7 for Saturday."""
    # isoweekday is 1 for Monday through 7 for Sunday, so this rotates Sunday
    # to the front.
    return (moment.isoweekday() % 7) + 1


class ClockService:
    """Sets the sign's clock on a schedule and whenever the link comes back."""

    def __init__(
        self,
        controller: SignController,
        *,
        interval_seconds: float = 3600.0,
        timezone: str | None = None,
        now: Callable[[], datetime] = _local_now,
    ) -> None:
        """Configure the sync. Nothing is sent until :meth:`sync` or :meth:`start`."""
        self._controller = controller
        self._interval = interval_seconds
        self._zone = ZoneInfo(timezone) if timezone else None
        self._now = now
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

        self.last_sync_at: datetime | None = None

    def current_time(self) -> datetime:
        """Return the time the sign should be told, in the configured zone."""
        moment = self._now()
        return moment.astimezone(self._zone) if self._zone else moment

    async def sync(self) -> datetime:
        """Set the sign's clock and day of week. Returns the time it was told."""
        moment = self.current_time()
        await self._controller.send_special(frames.set_time(moment.hour, moment.minute))
        await self._controller.send_special(frames.set_day_of_week(sign_day_of_week(moment)))
        self.last_sync_at = moment
        logger.info("sign clock set to %s", moment.strftime("%Y-%m-%d %H:%M %Z").strip())
        return moment

    async def start(self) -> None:
        """Begin syncing on a schedule."""
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop syncing."""
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def sync_quietly(self) -> None:
        """Sync, logging rather than raising if the sign is unreachable.

        This is what the reconnect hook and the periodic loop use. A clock sync
        that fails is worth a log line, not a crashed background task.
        """
        try:
            await self.sync()
        except (TransportError, OSError) as err:
            logger.warning("could not set the sign's clock: %s", err)

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            except TimeoutError:
                await self.sync_quietly()
            else:
                return
