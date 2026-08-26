"""Tests for the clock sync."""

from datetime import UTC, datetime, timedelta

import pytest

from readerboard.protocol import frames
from readerboard.services.clock import ClockService, sign_day_of_week
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
        moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert transport.packets == [
            frames.packet(frames.set_time(9, 5)),
            frames.packet(frames.set_day_of_week(3)),  # a Tuesday
        ]

    async def test_it_records_when_it_last_synced(self, controller):
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


class TestTimezone:
    async def test_an_explicit_zone_is_used(self, transport, controller):
        # 13:30 UTC is 09:30 in New York on this date.
        moment = datetime(2026, 8, 25, 13, 30, tzinfo=UTC)
        clock = ClockService(controller, timezone="America/New_York", now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(9, 30))

    async def test_the_zone_can_change_the_day(self, transport, controller):
        # Just after midnight UTC is still the previous evening in New York.
        moment = datetime(2026, 8, 26, 0, 30, tzinfo=UTC)  # a Wednesday in UTC
        clock = ClockService(controller, timezone="America/New_York", now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(20, 30))
        assert transport.packets[1] == frames.packet(frames.set_day_of_week(3))  # Tuesday

    async def test_no_zone_leaves_the_moment_alone(self, transport, controller):
        moment = datetime(2026, 8, 25, 9, 5, tzinfo=UTC)
        clock = ClockService(controller, now=lambda: moment)

        await clock.sync()

        assert transport.packets[0] == frames.packet(frames.set_time(9, 5))


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
    controller = SignController(transport, inter_packet_delay=0)
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
