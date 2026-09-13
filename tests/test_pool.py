"""Asking the sign how much memory it has, and refusing a pool it cannot hold.

This is the tier of the check that has a sign in front of it.
``tests/test_config.py`` covers the other one, which runs with no sign at all.

Every reply here is built the way ``tests/test_replies.py`` builds its own,
literally rather than through a helper that could be wrong in the same direction
as the parser.
"""

from __future__ import annotations

import logging

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.sign import pool
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
from readerboard.transport.serial_link import SerialTransport

FALLBACK = 5482


def framed(data: bytes) -> bytes:
    """Wrap a general information data field the way the sign wraps its answer.

    The EOT at the end is what tells the controller the answer is complete; a
    reply without one is a sign that started talking and stopped.
    """
    body = c.STX + c.COMMAND_WRITE_SPECIAL + c.SF_GENERAL_INFORMATION + data + c.ETX
    return (
        c.NUL * 20
        + c.SOH
        + c.SIGN_TYPE_RESPONSE
        + c.SIGN_ADDRESS_BROADCAST
        + body
        + b"%04X" % sum(body)
        + c.EOT
    )


def information(pool_size: bytes, free: bytes = b"0BB8") -> bytes:
    """Build a whole general information reply for a sign with a pool of this size."""
    return framed(b"1044-160B01931433M00" + pool_size + b"," + free)


class TestMeasure:
    async def test_it_reports_what_the_sign_says(self, controller, transport):
        # 156AH is 5482, which is what the sign on the bench answered with on
        # 2026-09-12 and is where ASSUMED_SIGN_MEMORY_POOL comes from.
        transport.replies = [information(b"156A")]

        assert await pool.measure(controller, fallback=FALLBACK) == 5482

    async def test_it_asks_the_sign_for_general_information(self, controller, transport):
        transport.replies = [information(b"156A")]

        await pool.measure(controller, fallback=FALLBACK)

        assert transport.packets == [frames.packet(frames.read_general_information())]

    async def test_the_signs_own_figure_wins_over_the_assumption(self, controller, transport):
        # Either way. A sign with more memory than the assumption may use it.
        transport.replies = [information(b"4000")]

        assert await pool.measure(controller, fallback=FALLBACK) == 0x4000

    async def test_a_silent_sign_falls_back_rather_than_failing(
        self, controller, transport, caplog
    ):
        # Nothing scripted, so the fake says nothing, which is also what a
        # sign simulator and an unplugged cable look like from here.
        with caplog.at_level(logging.WARNING):
            assert await pool.measure(controller, fallback=FALLBACK) == FALLBACK

        assert "could not read the sign's memory pool" in caplog.text
        # The figure it fell back to is named, so the log says what was assumed
        # rather than only that something was.
        assert "5482" in caplog.text

    async def test_a_reply_it_cannot_read_falls_back_too(self, controller, transport, caplog):
        # A frame that arrives whole and parses as nothing.
        transport.replies = [framed(b"not a general information reply")]

        with caplog.at_level(logging.WARNING):
            assert await pool.measure(controller, fallback=FALLBACK) == FALLBACK

        assert "could not read the sign's memory pool" in caplog.text

    async def test_a_loop_url_falls_back_on_its_own_echo(self, caplog):
        """The real pyserial path, not the fake, because the echo is the point.

        ``loop://`` hands back whatever was written, so the question comes home
        as its own answer: a whole frame, ending in the EOT the reader waits
        for, carrying the read command code rather than the write code the sign
        echoes. That parses as nothing and falls back at once. Every other test
        here reasons about ``loop://`` through a fake; this one runs it.
        """
        link = SerialTransport("loop://", timeout=1.0)
        # The controller's own sleep, not the instant one the fixtures hand out:
        # the echo has to come back inside a real poll for this to prove
        # anything, and it does, so nothing here waits.
        controller = SignController(link, inter_packet_delay=0)
        await controller.start()
        try:
            with caplog.at_level(logging.WARNING):
                assert await pool.measure(controller, fallback=FALLBACK) == FALLBACK
        finally:
            await controller.stop()

        # The echo came back, so this is a reply that would not parse rather
        # than a sign that never spoke. Both fall back; only one of them means
        # the three second deadline was waited out.
        assert "could not read the sign's memory pool" in caplog.text
        assert "did not answer" not in caplog.text

    async def test_a_failure_nobody_planned_for_falls_back_rather_than_failing(
        self, controller, transport, caplog, monkeypatch
    ):
        # A bug in the asking must not stop the service starting. The lifespan
        # learned that from an alert that raised past its handler and left the
        # service unable to start at all.
        transport.replies = [information(b"156A")]

        def explode(_reply: bytes) -> object:
            raise RuntimeError("something nobody named")

        monkeypatch.setattr(pool, "parse_general_information", explode)

        with caplog.at_level(logging.ERROR):
            assert await pool.measure(controller, fallback=FALLBACK) == FALLBACK

        assert "did not plan for" in caplog.text
        assert "something nobody named" in caplog.text

    async def test_a_pool_of_zero_is_read_as_no_answer(self, controller, transport, caplog):
        # It parses, and it is not a size any sign has. Refusing to start over
        # it would be refusing to start over line noise.
        transport.replies = [information(b"0000", free=b"0000")]

        with caplog.at_level(logging.WARNING):
            assert await pool.measure(controller, fallback=FALLBACK) == FALLBACK

        assert "not a size any sign has" in caplog.text


class TestCheckFits:
    def test_a_pool_that_fits_passes_quietly(self):
        pool.check_fits(Layout(8, 256, 8, 32), 5482)

    def test_the_boundary_is_inclusive(self):
        layout = Layout(8, 256, 8, 32)
        claimed = frames.memory_claimed(layout.allocations())

        pool.check_fits(layout, claimed)
        with pytest.raises(pool.PoolTooLarge):
            pool.check_fits(layout, claimed - 1)

    def test_a_pool_the_sign_cannot_hold_is_refused(self):
        with pytest.raises(pool.PoolTooLarge):
            pool.check_fits(Layout(26, 200, 8, 32), 5482)

    def test_the_message_names_the_configuration_what_it_needs_and_what_there_is(self):
        with pytest.raises(pool.PoolTooLarge) as raised:
            pool.check_fits(Layout(26, 200, 8, 32), 1024)

        message = str(raised.value)
        assert "slot_count 26" in message
        assert "slot_capacity 200" in message
        assert "variable_count 8" in message
        assert "variable_capacity 32" in message
        assert "%d bytes" % frames.memory_claimed(Layout(26, 200, 8, 32).allocations()) in message
        assert "the sign has 1024" in message
        # And says that nothing has been lost yet, because the thing somebody
        # reading this fears is that the sign has already been erased.
        assert "still holds" in message

    def test_the_variables_count_against_the_pool(self):
        # 26 variables at the protocol's 125 byte ceiling is 3588 bytes, which
        # is most of this sign, so they are never a rounding error.
        pool.check_fits(Layout(4, 256, 26, 125), 5482)
        with pytest.raises(pool.PoolTooLarge):
            pool.check_fits(Layout(8, 256, 26, 125), 5482)
