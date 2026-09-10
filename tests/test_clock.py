"""Tests for the clock sync."""

from datetime import UTC, datetime, timedelta

import pytest

from readerboard.protocol import frames
from readerboard.services.clock import CLOCK_LEAD, ClockService, sign_day_of_week
from readerboard.sign.controller import SignController
from readerboard.transport.base import TransportError
from readerboard.transport.fake import FakeTransport


class TestSignDayOfWeek:
    """The sign numbers 1 for Sunday through 7 for Saturday.

    Easy to get wrong in both the base and the starting day, so it is pinned
    here rather than left to a comment.
    """

    @pytest.mark.parametrize(
        "date,expected",
        [
            (datetime(2026, 8, 23), 1),  # Sunday
            (datetime(2026, 8, 24), 2),  # Monday
            (datetime(2026, 8, 25), 3),  # Tuesday
            (datetime(2026, 8, 26), 4),  # Wednesday
            (datetime(2026, 8, 27), 5),  # Thursday
            (datetime(2026, 8, 28), 6),  # Friday
            (datetime(2026, 8, 29), 7),  # Saturday
        ],
    )
    def test_every_day_maps(self, date, expected):
        assert sign_day_of_week(date) == expected


class TestSync:
    async def test_it_sends_the_time_and_the_day(self, transport, controller):
        # 9:06, not 9:05. The sign is set a minute ahead on purpose; see
        # CLOCK_LEAD for why, and TestTheLead below for what it buys.
        moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert transport.packets == [
            frames.packet(frames.set_time(9, 6)),
            frames.packet(frames.set_day_of_week(3)),  # a Tuesday
        ]

    async def test_it_records_when_it_last_synced(self, controller):
        # When the sync happened, not what the sign was told. Those differ now,
        # and this is the one that answers "is the clock being kept up to date".
        moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        assert clock.last_sync_at is None
        await clock.sync()
        assert clock.last_sync_at == moment

    async def test_syncing_twice_is_not_suppressed(self, transport, controller):
        # The point of a resync is to correct drift nobody can see, so sending
        # the same time again has to actually reach the sign.
        moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()
        await clock.sync()

        assert transport.write_count == 4


class TestTheLead:
    """The sign is set a minute fast, deliberately and always.

    Set Time carries HHMM and no seconds, so a sign told the current minute
    starts that minute from scratch and then runs behind for the whole of it, by
    up to fifty-nine seconds. The error is one-sided: the sign is never early,
    only ever late, and this sign's drift is slow on top of that.

    Leading by a minute moves the same one-sided error to the other side. Worst
    case the sign reads a minute fast, best case exact, never slow.
    """

    async def test_the_lead_is_exactly_one_minute(self, transport, controller):
        moment = datetime(2026, 8, 25, 9, 5, 45, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert timedelta(minutes=1) == CLOCK_LEAD
        assert transport.packets[0] == frames.packet(frames.set_time(9, 6))

    async def test_the_seconds_within_the_minute_do_not_change_what_is_sent(
        self, transport, controller
    ):
        """Whatever second it is, the sign is told the next minute.

        This is the property that makes the sign never slow. At :01 the lead is
        nearly a whole minute of being fast; at :59 it is nearly exact. Neither
        is ever behind.
        """
        for second in (0, 1, 30, 59):
            transport.packets.clear()
            moment = datetime(2026, 8, 25, 9, 5, second, tzinfo=UTC)
            clock = ClockService(controller, now=lambda m=moment: m)

            await clock.sync()

            assert transport.packets[0] == frames.packet(frames.set_time(9, 6)), second

    async def test_it_rolls_the_hour(self, transport, controller):
        moment = datetime(2026, 8, 25, 9, 59, 30, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(10, 0))

    async def test_the_last_minute_of_the_day_takes_the_next_day_with_it(
        self, transport, controller
    ):
        """At 23:59 the sign is told 00:00, so it must be told tomorrow's day.

        One minute a day this matters, and getting it wrong would put the wrong
        day of the week on the sign for that minute. The day is derived from the
        shifted moment for exactly this reason.
        """
        moment = datetime(2026, 8, 25, 23, 59, 30, tzinfo=UTC)  # a Tuesday
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(0, 0))
        assert transport.packets[1] == frames.packet(frames.set_day_of_week(4))  # Wednesday

    async def test_the_lead_is_added_before_the_zone_conversion(self, transport, controller):
        """The morning the clocks go forward, wall-clock arithmetic breaks.

        In New York on 2026-03-08 the local time goes from 01:59:59 straight to
        03:00:00. Adding a minute to the local wall clock at 01:59:30 gives
        02:00, a time that does not exist that day, and the sign would be told
        it. Adding the minute to the instant and converting afterwards gives
        03:00, which is what the clock on the wall will actually say.

        Pinned because the wrong order passes every other test in this file.
        """
        moment = datetime(2026, 3, 8, 6, 59, 30, tzinfo=UTC)  # 01:59:30 EST
        clock = ClockService(controller, timezone="America/New_York", now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(3, 0))

    async def test_what_it_reports_is_the_real_time_not_the_lead(self, controller):
        # A caller polling this wants to know the clock is being kept up to
        # date. Reporting a time in the future would read as a bug.
        moment = datetime(2026, 8, 25, 9, 5, 45, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        assert await clock.sync() == moment
        assert clock.last_sync_at == moment

    async def test_current_time_is_still_the_actual_time(self, controller):
        moment = datetime(2026, 8, 25, 9, 5, 45, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        assert clock.current_time() == moment
        assert clock.time_to_send() == moment + CLOCK_LEAD


class TestTimezone:
    async def test_an_explicit_zone_is_used(self, transport, controller):
        # 13:30 UTC is 09:30 in New York on this date.
        moment = datetime(2026, 8, 25, 13, 30, tzinfo=UTC)
        clock = ClockService(controller, timezone="America/New_York", now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(9, 31))

    async def test_the_zone_can_change_the_day(self, transport, controller):
        # Just after midnight UTC is still the previous evening in New York.
        moment = datetime(2026, 8, 26, 0, 30, tzinfo=UTC)  # a Wednesday in UTC
        clock = ClockService(controller, timezone="America/New_York", now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(20, 31))
        assert transport.packets[1] == frames.packet(frames.set_day_of_week(3))  # Tuesday

    async def test_no_zone_leaves_the_moment_alone(self, transport, controller):
        moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(9, 6))


class TestFailure:
    async def test_sync_raises_when_the_sign_is_unreachable(self, controller, transport):
        transport.fail_with = "cable unplugged"
        clock = ClockService(controller, now=lambda: datetime(2026, 8, 25, 9, 5, tzinfo=UTC))

        with pytest.raises(TransportError):
            await clock.sync()

    async def test_the_quiet_form_logs_instead_of_raising(self, controller, transport, caplog):
        # A failed clock sync in a background loop is a log line, not a crashed
        # task that silently stops syncing for good.
        transport.fail_with = "cable unplugged"
        clock = ClockService(controller, now=lambda: datetime(2026, 8, 25, 9, 5, tzinfo=UTC))

        await clock.sync_quietly()

        assert "could not set the sign's clock" in caplog.text


async def test_it_syncs_when_the_link_comes_back():
    """The reconnect trigger is the one a schedule alone cannot provide."""
    transport = FakeTransport(open_fails_with="no route to host")
    controller = SignController(transport, inter_packet_delay=0, settle=False)
    moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
    clock = ClockService(controller, now=lambda: moment)
    controller.on_reconnect(clock.sync_quietly)

    await controller.start()
    try:
        assert clock.last_sync_at is None

        # The adapter comes back.
        transport.open_fails_with = None
        await controller._connect()

        assert clock.last_sync_at == moment
    finally:
        await controller.stop()


async def test_the_scheduled_loop_syncs_on_its_interval(controller, transport):
    moments = iter(
        datetime(2026, 8, 25, 9, 5, tzinfo=UTC) + timedelta(minutes=n) for n in range(100)
    )
    clock = ClockService(controller, interval_seconds=0.01, now=lambda: next(moments))

    await clock.start()
    try:
        for _ in range(200):
            if clock.last_sync_at is not None:
                break
            await _tick()
    finally:
        await clock.stop()

    assert clock.last_sync_at is not None
    assert transport.write_count >= 2


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.005)
