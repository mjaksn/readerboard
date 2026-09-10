"""Keeping the sign's clock right.

The sign has its own battery-backed clock and it drifts. It also loses the time
when it loses power for long enough, and a sign showing ``<time>`` with a wrong
clock is worse than one showing nothing, because it looks fine.

So the clock is set on startup, once an hour, and on every reconnect. The
reconnect trigger is the one that earns its keep: a scheduled sync alone leaves
a sign that came back at ten past the hour wrong until the next sync, and the
link returning is the closest signal available to "the sign may just have been
power cycled".

The time it is told is a minute ahead of the real one, always. That is not a
correction for drift and it is not a bug; ``CLOCK_LEAD`` below has the whole
reasoning, which comes down to Set Time having no seconds field.

``now`` is injected rather than mocked, so the tests can be about what gets sent
at a given time rather than about patching the clock out from under the code.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from readerboard.protocol import frames
from readerboard.sign.controller import SignController
from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)

# The sign is deliberately set one minute fast, and this is not a fudge factor.
#
# Set Time takes four digits, HHMM. There is no seconds field, so a sign told
# "14:33" at 14:33:45 does not start the minute forty-five seconds in; it starts
# it now. It then rolls to 14:34 at 14:34:45. The sign is behind for the whole
# minute, by up to fifty-nine seconds, and only ever behind: the error is
# one-sided because the seconds it was set at are thrown away.
#
# Drift is on top of that and this sign's runs slow, so the two compound.
#
# Adding a minute moves the same one-sided error to the other side. Worst case
# the sign reads a minute fast, best case it is exact, and it is never slow. A
# clock that is a little fast is a much smaller irritation than one that spends
# most of every minute showing the time that has just gone.
#
# The day of the week is derived from the same shifted moment rather than from
# now, which matters for one minute a day: at 23:59:30 the sign is told 00:00,
# and it must be told tomorrow's day to go with it.
CLOCK_LEAD = timedelta(minutes=1)


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
        """Return what the time actually is, in the configured zone."""
        return self._in_zone(self._now())

    def time_to_send(self) -> datetime:
        """Return the time to set the sign to, which is ``CLOCK_LEAD`` ahead of now."""
        return self._in_zone(self._now() + CLOCK_LEAD)

    def _in_zone(self, moment: datetime) -> datetime:
        """Put a moment in the configured zone, or leave it alone if there is none."""
        return moment.astimezone(self._zone) if self._zone else moment

    async def sync(self) -> datetime:
        """Set the sign's clock and day of week. Returns when the sync happened.

        The clock is read once here rather than through :meth:`current_time` and
        :meth:`time_to_send` separately, so that the two cannot land either side
        of a second boundary and disagree about which minute it is.

        The lead is added before the zone conversion, not after. Adding it after
        would be wall-clock arithmetic, and at 01:59:30 on the morning the clocks
        go forward that produces 02:00, a time which does not exist that day.
        Adding it to the instant and then converting is right on every day of the
        year.
        """
        now = self._now()
        moment = self._in_zone(now)
        ahead = self._in_zone(now + CLOCK_LEAD)

        await self._controller.send_special(frames.set_time(ahead.hour, ahead.minute))
        await self._controller.send_special(frames.set_day_of_week(sign_day_of_week(ahead)))
        self.last_sync_at = moment
        logger.info(
            "sign clock set to %s, which is %s ahead of %s",
            ahead.strftime("%Y-%m-%d %H:%M %Z").strip(),
            CLOCK_LEAD,
            moment.strftime("%H:%M:%S"),
        )
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
