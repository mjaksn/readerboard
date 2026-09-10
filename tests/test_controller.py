"""Tests for the single writer: ordering, suppression, and failure handling."""

import asyncio

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import render
from readerboard.sign.controller import (
    MEMORY_CLEAR_SETTLE_SECONDS,
    RESET_SETTLE_SECONDS,
    SignController,
)
from readerboard.transport.base import TransportError
from readerboard.transport.fake import FakeTransport


async def test_a_write_reaches_the_transport_as_a_full_packet():
    transport = FakeTransport()
    controller = SignController(transport, inter_packet_delay=0)

    written = await controller.write_text_file(b"A", render("HI"))

    assert written is True
    assert transport.last_packet == frames.packet(frames.write_text_file(b"A", b"HI"))


class TestSuppression:
    async def test_writing_the_same_bytes_twice_only_writes_once(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        assert await controller.write_text_file(b"A", b"HI") is True
        assert await controller.write_text_file(b"A", b"HI") is False

        assert transport.write_count == 1
        assert controller.suppressed == 1

    async def test_changed_content_is_written(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.write_text_file(b"A", b"HI")
        assert await controller.write_text_file(b"A", b"THERE") is True
        assert transport.write_count == 2

    async def test_a_changed_mode_counts_as_changed(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.write_text_file(b"A", b"HI", mode=c.MODE_HOLD)
        assert await controller.write_text_file(b"A", b"HI", mode=c.MODE_ROTATE) is True

    async def test_suppression_is_per_file(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.write_text_file(b"A", b"HI")
        assert await controller.write_text_file(b"B", b"HI") is True
        assert transport.write_count == 2

    async def test_the_run_sequence_is_suppressed_when_unchanged(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        assert await controller.set_run_sequence([b"A", b"B"]) is True
        assert await controller.set_run_sequence([b"A", b"B"]) is False
        assert await controller.set_run_sequence([b"B", b"A"]) is True

    async def test_a_control_command_is_never_suppressed(self):
        # Setting the clock to the value it already holds is still worth doing,
        # because the point is to correct drift nobody can see.
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.send_special(frames.set_time(9, 30))
        await controller.send_special(frames.set_time(9, 30))
        assert transport.write_count == 2


class TestMemoryConfiguration:
    async def test_it_is_written_and_forgets_what_the_sign_held(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.write_text_file(b"A", b"HI")
        await controller.apply_memory_config([frames.FileAllocation(b"A", 256)])

        # The sign has just been erased, so the same bytes must go again.
        assert await controller.write_text_file(b"A", b"HI") is True

    async def test_memory_is_cleared_before_it_is_configured(self):
        # A BetaBrite Classic on an Ethernet adapter stored a configuration,
        # read it back correctly, and displayed nothing from it until a bare E$
        # clear was sent first. So the clear is not optional and it comes first.
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        allocations = [frames.FileAllocation(b"A", 256)]
        await controller.apply_memory_config(allocations)

        assert transport.packets == [
            frames.packet(frames.clear_memory()),
            frames.packet(frames.set_memory_config(allocations)),
        ]

    async def test_it_warns_that_the_sign_will_be_cleared(self, caplog):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.apply_memory_config([frames.FileAllocation(b"A", 256)])

        assert "clears every message" in caplog.text


class TestConcurrency:
    async def test_concurrent_writes_are_serialised(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        bodies = [b"MESSAGE %d" % index for index in range(20)]
        await asyncio.gather(
            *(controller.write_text_file(b"A", body) for body in bodies)
        )

        # Every write is distinct, so none should have been suppressed, and the
        # transport must have seen exactly one packet per write.
        assert transport.write_count == len(bodies)
        assert len(transport.packets) == len(bodies)

    async def test_the_inter_packet_delay_is_honoured(self):
        """Assert what the controller waited for, not how long the test took.

        An earlier version of this timed two writes and asserted the elapsed
        wall clock was at least twice the delay. That fails on Windows, whose
        timer granularity is around 15ms: two 50ms sleeps came back in 94ms,
        six short, which says nothing about whether the delay was applied.
        Recording the requested durations tests the same contract and cannot
        flake.
        """
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0.05, sleep=record)

        await controller.write_text_file(b"A", b"ONE")
        await controller.write_text_file(b"A", b"TWO")

        assert slept == [0.05, 0.05]

    async def test_no_delay_is_requested_when_it_is_configured_away(self):
        """A zero delay must not become a zero-length sleep on every packet."""
        slept: list[float] = []

        async def record(seconds: float) -> None:
            slept.append(seconds)

        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0, sleep=record)

        await controller.write_text_file(b"A", b"ONE")
        await controller.write_text_file(b"A", b"TWO")

        assert slept == []


def recording_sleep(recorded: list[float]):
    """Build a sleep that records what it was asked for and yields to the loop.

    The yield is what makes the locking tests below mean anything: without it a
    task holding the lock never gives another task the chance to try for it, so
    a missing lock would look exactly like a held one.
    """

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)
        await asyncio.sleep(0)

    return sleep


class TestWaitingForAReset:
    async def test_a_resetting_command_waits_for_the_sign_to_come_back(self):
        slept: list[float] = []
        controller = SignController(
            FakeTransport(), inter_packet_delay=0.05, sleep=recording_sleep(slept)
        )

        await controller.send_special(frames.soft_reset(), settle=True)

        assert RESET_SETTLE_SECONDS in slept

    async def test_an_ordinary_command_does_not_wait(self):
        # Setting the clock does not restart the sign, so nothing should stall.
        slept: list[float] = []
        controller = SignController(
            FakeTransport(), inter_packet_delay=0.05, sleep=recording_sleep(slept)
        )

        await controller.send_special(frames.set_time(9, 30))

        assert RESET_SETTLE_SECONDS not in slept

    async def test_no_settle_is_requested_when_pacing_is_configured_away(self):
        # A zero delay is a fake or a simulator, which has no reset to wait
        # through, so the recovery path must not stall a test for ten seconds.
        slept: list[float] = []
        controller = SignController(
            FakeTransport(), inter_packet_delay=0, sleep=recording_sleep(slept)
        )

        await controller.send_special(frames.soft_reset(), settle=True)

        assert slept == []

    async def test_a_reconfiguration_waits_for_the_sign_before_it_returns(self):
        # Otherwise whoever rebuilds the display next writes into the reset.
        slept: list[float] = []
        controller = SignController(
            FakeTransport(), inter_packet_delay=0.05, sleep=recording_sleep(slept)
        )

        await controller.apply_memory_config([frames.FileAllocation(b"A", 256)])

        assert MEMORY_CLEAR_SETTLE_SECONDS in slept
        assert RESET_SETTLE_SECONDS in slept


class TestNothingElseWritesWhileTheSignIsResetting:
    """A reset holds the lock throughout, not merely around each packet.

    The sign is deaf from the moment a reset reaches it until its diagnostics
    finish, and a write arriving in that window is not refused: the transport
    accepts it, the suppression cache records it as delivered, and the caller is
    told it worked. The message is simply never on the sign. So a reset has to
    make every other writer queue behind it, which means holding the lock across
    the settle rather than only across each send.
    """

    async def completes(self, task, seconds: float = 0.5) -> bool:
        """Whether ``task`` finishes within ``seconds``, telling blocked from slow.

        A couple of ``asyncio.sleep(0)`` yields is not enough to answer this:
        the write goes through ``asyncio.to_thread``, which needs real thread
        scheduling, so an unblocked write is still unfinished after two ticks
        and the test would pass with the lock removed. That version of this test
        was written, checked against the broken code, and did pass. Waiting on
        the task is what actually separates "the lock stopped it" from "the
        thread had not got there yet".
        """
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=seconds)
            return True
        except TimeoutError:
            return False

    def held_at(self, target: float):
        """Build a sleep that parks inside the settle for ``target`` until released.

        Asserting on packet order alone would prove nothing here: a lock-free
        settle produces the same order, because the reset's own packet still
        goes first. What differs is whether a write can *complete* while the
        sign is deaf, so the test has to stop time inside that window and look.
        """
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sleep(seconds: float) -> None:
            if seconds == target:
                entered.set()
                await release.wait()
            else:
                await asyncio.sleep(0)

        return sleep, entered, release

    async def test_a_write_cannot_land_between_the_clear_and_the_configuration(self):
        sleep, entered, release = self.held_at(MEMORY_CLEAR_SETTLE_SECONDS)
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0.01, sleep=sleep)

        reset = asyncio.create_task(
            controller.apply_memory_config([frames.FileAllocation(b"A", 256)])
        )
        await entered.wait()  # the clear has gone; the sign is resetting

        write = asyncio.create_task(controller.write_text_file(b"A", b"HI"))

        assert not await self.completes(write)
        assert transport.packets == [frames.packet(frames.clear_memory())]

        release.set()
        await asyncio.gather(reset, write)
        assert transport.packets[-1] == frames.packet(
            frames.write_text_file(b"A", b"HI")
        )

    async def test_a_write_cannot_land_during_a_soft_reset(self):
        sleep, entered, release = self.held_at(RESET_SETTLE_SECONDS)
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0.01, sleep=sleep)

        reset = asyncio.create_task(
            controller.send_special(frames.soft_reset(), settle=True)
        )
        await entered.wait()  # the sign is running its diagnostics

        write = asyncio.create_task(controller.write_text_file(b"A", b"HI"))

        # This is the regression. Without the lock the write goes out here, the
        # sign drops it, and the suppression cache records it as delivered.
        assert not await self.completes(write)
        assert transport.packets == [frames.packet(frames.soft_reset())]

        release.set()
        await asyncio.gather(reset, write)
        assert transport.packets == [
            frames.packet(frames.soft_reset()),
            frames.packet(frames.write_text_file(b"A", b"HI")),
        ]


class TestTheLinkWatcherSurvives:
    """It is the only thing that opens the link, so nothing may kill it.

    A write used to open one lazily and no longer does. If this task dies the
    service answers 503 until somebody restarts it, however healthy the sign
    becomes. It died once on an arithmetic error out of the transport's own
    backoff, which is not a TransportError and so matched nothing that was being
    caught on the way out.
    """

    def exploding(self, error: Exception) -> FakeTransport:
        transport = FakeTransport()

        def ensure_open() -> None:
            raise error

        transport.ensure_open = ensure_open  # type: ignore[method-assign]
        return transport

    async def test_startup_survives_an_unexpected_error_from_the_link(self, caplog):
        controller = SignController(
            self.exploding(OverflowError("int too large to convert to float")),
            inter_packet_delay=0,
        )

        with caplog.at_level("ERROR"):
            await controller.start()  # must not raise
        try:
            assert controller._reconnect_task is not None
            assert not controller._reconnect_task.done()
        finally:
            await controller.stop()

    async def test_the_watcher_keeps_going_after_an_unexpected_error(self, caplog):
        controller = SignController(
            self.exploding(OverflowError("int too large to convert to float")),
            inter_packet_delay=0,
        )

        with caplog.at_level("ERROR"):
            await controller.start()
            # Long enough for the loop to take at least one turn and raise.
            await asyncio.sleep(0.05)
        try:
            task = controller._reconnect_task
            assert task is not None
            assert not task.done(), "the watcher died, so the link can never reopen"
        finally:
            await controller.stop()


class TestFailures:
    async def test_a_failed_write_raises_rather_than_reporting_success(self):
        # Reported as a 200 with an error string in the body, a dead link is
        # indistinguishable from success to anything reading the status code.
        transport = FakeTransport(fail_with="cable unplugged")
        controller = SignController(transport, inter_packet_delay=0)

        with pytest.raises(TransportError, match="cable unplugged"):
            await controller.write_text_file(b"A", b"HI")

        assert controller.last_error == "cable unplugged"

    async def test_a_failed_write_does_not_leave_stale_suppression_state(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)
        await controller.write_text_file(b"A", b"HI")

        transport.fail_with = "cable unplugged"
        with pytest.raises(TransportError):
            await controller.write_text_file(b"A", b"THERE")

        # Once the link is back, the earlier content must not be assumed intact.
        transport.fail_with = None
        assert await controller.write_text_file(b"A", b"HI") is True

    async def test_startup_survives_a_sign_that_is_not_there(self, caplog):
        transport = FakeTransport(open_fails_with="no route to host")
        controller = SignController(transport, inter_packet_delay=0)

        await controller.start()
        try:
            assert not controller.is_connected
            assert "sign not reachable at startup" in caplog.text
        finally:
            await controller.stop()


class TestReconnectHooks:
    async def test_a_hook_runs_when_the_link_comes_up(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)
        calls: list[int] = []

        controller.on_reconnect(lambda: _record(calls))

        await controller.start()
        try:
            assert calls == [1]
        finally:
            await controller.stop()

    async def test_a_failing_hook_does_not_stop_the_others(self, caplog):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)
        calls: list[int] = []

        controller.on_reconnect(_boom)
        controller.on_reconnect(lambda: _record(calls))

        await controller.start()
        try:
            assert calls == [1]
            assert "reconnect hook failed" in caplog.text
        finally:
            await controller.stop()


async def _record(calls: list[int]) -> None:
    calls.append(1)


async def _boom() -> None:
    raise RuntimeError("hook exploded")


async def test_priority_write_and_release():
    transport = FakeTransport()
    controller = SignController(transport, inter_packet_delay=0)

    await controller.write_priority(render("<red>ALERT"))
    assert transport.last_packet == frames.packet(
        frames.write_text_file(c.FILE_PRIORITY, c.TEXT_COLOR_RED + b"ALERT")
    )

    await controller.clear_priority()
    assert transport.last_packet == frames.packet(frames.clear_priority_file())


class TestForcedWrite:
    async def test_a_forced_write_ignores_suppression(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.write_text_file(b"A", b"HI")
        assert await controller.write_text_file(b"A", b"HI") is False
        assert await controller.write_text_file(b"A", b"HI", force=True) is True

        assert transport.write_count == 2

    async def test_a_forced_write_still_updates_the_cache(self):
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0)

        await controller.write_text_file(b"A", b"HI", force=True)
        # The cache must reflect what was just written, or suppression would
        # stop working for every later write to this file.
        assert await controller.write_text_file(b"A", b"HI") is False
