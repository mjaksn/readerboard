"""Tests for the single writer: ordering, suppression, and failure handling."""

import asyncio

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import render
from readerboard.sign.controller import SignController
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
        transport = FakeTransport()
        controller = SignController(transport, inter_packet_delay=0.05)

        started = asyncio.get_running_loop().time()
        await controller.write_text_file(b"A", b"ONE")
        await controller.write_text_file(b"A", b"TWO")
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed >= 0.1


class TestFailures:
    async def test_a_failed_write_raises_rather_than_reporting_success(self):
        # The old server turned this into HTTP 200 with an error string, so a
        # dead link looked exactly like success to Home Assistant.
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
