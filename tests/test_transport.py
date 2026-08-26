"""Tests for the transport layer.

The fake covers the behaviour the rest of the service depends on. The loop://
tests are there so that the real ``serial_for_url`` path is exercised too, since
a fake that is never checked against the thing it stands in for is just a second
implementation of the same guesswork.
"""

import pytest

from readerboard.transport.base import Transport, TransportError
from readerboard.transport.fake import FakeTransport
from readerboard.transport.serial_link import SerialTransport


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestFakeTransport:
    def test_it_satisfies_the_transport_protocol(self):
        assert isinstance(FakeTransport(), Transport)

    def test_writing_records_the_packet_and_opens_the_link(self):
        transport = FakeTransport()
        transport.write(b"HELLO")
        assert transport.packets == [b"HELLO"]
        assert transport.is_open
        assert transport.open_count == 1

    def test_a_failing_write_raises_and_drops_the_link(self):
        transport = FakeTransport(fail_with="cable unplugged")
        with pytest.raises(TransportError, match="cable unplugged"):
            transport.write(b"HELLO")
        assert transport.packets == []
        assert not transport.is_open

    def test_a_failing_open_raises(self):
        transport = FakeTransport(open_fails_with="no route to host")
        with pytest.raises(TransportError, match="no route to host"):
            transport.ensure_open()

    def test_last_packet_complains_when_nothing_was_written(self):
        with pytest.raises(AssertionError, match="nothing has been written"):
            _ = FakeTransport().last_packet


class TestSerialTransportOverLoopback:
    """Exercise the real serial_for_url code path."""

    def test_it_satisfies_the_transport_protocol(self):
        assert isinstance(SerialTransport("loop://"), Transport)

    def test_nothing_is_opened_until_asked(self):
        transport = SerialTransport("loop://")
        assert not transport.is_open

    def test_writing_opens_the_link_and_the_bytes_come_back(self):
        transport = SerialTransport("loop://", timeout=1.0)
        try:
            transport.write(b"HELLO")
            assert transport.is_open
            # loop:// echoes what is written, so this proves the bytes really
            # went through pyserial rather than into a fake.
            assert transport._port is not None
            assert transport._port.read(5) == b"HELLO"
        finally:
            transport.close()

    def test_closing_an_already_closed_link_is_harmless(self):
        transport = SerialTransport("loop://")
        transport.close()
        transport.close()
        assert not transport.is_open

    def test_the_description_is_the_url(self):
        assert SerialTransport("socket://sign.example:4001").description == (
            "socket://sign.example:4001"
        )


class TestBackoff:
    """A link that will not open must not be retried on every request."""

    def unopenable(self, clock: Clock) -> SerialTransport:
        # pyserial rejects an unknown URL scheme outright, which gives a
        # deterministic open failure with no network involved.
        return SerialTransport(
            "nosuchscheme://sign",
            backoff_initial=1.0,
            backoff_max=8.0,
            monotonic=clock,
        )

    def test_the_first_attempt_reports_why_it_failed(self):
        transport = self.unopenable(Clock())
        with pytest.raises(TransportError, match="could not open"):
            transport.ensure_open()

    def test_a_second_attempt_inside_the_window_does_not_touch_the_device(self):
        clock = Clock()
        transport = self.unopenable(clock)

        with pytest.raises(TransportError, match="could not open"):
            transport.ensure_open()
        with pytest.raises(TransportError, match="next attempt in"):
            transport.ensure_open()

    def test_the_window_widens_and_then_caps(self):
        clock = Clock()
        transport = self.unopenable(clock)
        delays = []

        for _ in range(6):
            with pytest.raises(TransportError):
                transport.ensure_open()
            delays.append(round(transport.seconds_until_retry(), 3))
            clock.advance(delays[-1])

        assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]

    def test_the_window_expires(self):
        clock = Clock()
        transport = self.unopenable(clock)

        with pytest.raises(TransportError):
            transport.ensure_open()
        assert transport.seconds_until_retry() == pytest.approx(1.0)

        clock.advance(1.0)
        assert transport.seconds_until_retry() == 0.0
        with pytest.raises(TransportError, match="could not open"):
            transport.ensure_open()

    def test_writing_while_down_fails_fast_rather_than_blocking(self):
        clock = Clock()
        transport = self.unopenable(clock)

        with pytest.raises(TransportError):
            transport.write(b"HELLO")
        with pytest.raises(TransportError, match="next attempt in"):
            transport.write(b"HELLO")

    def test_a_successful_open_clears_the_backoff(self):
        clock = Clock()
        transport = SerialTransport("loop://", monotonic=clock)
        try:
            transport.ensure_open()
            assert transport.seconds_until_retry() == 0.0
            assert transport.last_error is None
        finally:
            transport.close()
