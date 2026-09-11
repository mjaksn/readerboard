"""Tests for the transport layer.

The fake covers the behaviour the rest of the service depends on. The loop://
tests are there so that the real ``serial_for_url`` path is exercised too, since
a fake that is never checked against the thing it stands in for is just a second
implementation of the same guesswork.
"""

import socket
import threading
import time

import pytest

from readerboard.transport.base import Transport, TransportError
from readerboard.transport.fake import FakeTransport
from readerboard.transport.serial_link import READ_LIMIT_BYTES, SerialTransport


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

    def test_writing_over_an_open_link_puts_the_bytes_through(self):
        transport = SerialTransport("loop://", timeout=1.0)
        try:
            transport.ensure_open()
            transport.write(b"HELLO")
            assert transport.is_open
            # loop:// echoes what is written, so this proves the bytes really
            # went through pyserial rather than into a fake.
            assert transport._port is not None
            assert transport._port.read(5) == b"HELLO"
        finally:
            transport.close()

    def test_writing_a_link_that_is_down_fails_at_once_without_opening(self):
        # The link is not opened by a write. loop:// would open instantly if it
        # were, so a write that raises "is down" rather than echoing is the proof
        # that opening is left to the reconnect loop and the caller is not made
        # to wait out a connection attempt.
        transport = SerialTransport("loop://")
        assert not transport.is_open
        with pytest.raises(TransportError, match="is down"):
            transport.write(b"HELLO")
        assert not transport.is_open

    def test_closing_an_already_closed_link_is_harmless(self):
        transport = SerialTransport("loop://")
        transport.close()
        transport.close()
        assert not transport.is_open

    def test_the_description_is_the_url(self):
        assert SerialTransport("socket://sign.example:4001").description == (
            "socket://sign.example:4001"
        )


class TestSerialTransportOverASocket:
    """socket:// is how the sign is reached through an adapter, and it counts differently.

    pyserial's ``in_waiting`` over a socket is 1 while anything is waiting, not
    how much, which loop:// cannot show because it reports the real count. A
    read that trusted it took one byte a poll, and a reply the length of the
    general information ran out of time before its end.
    """

    REPLY = b"\x00" * 20 + b"\x01000\x02E\x221044-160B01931433M004000,0BB8\x030BA4\x04"

    def test_everything_waiting_is_read_at_once(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        sent = threading.Event()
        done = threading.Event()

        def serve() -> None:
            conn, _ = listener.accept()
            with conn:
                # Answer only when asked, as the sign does. pyserial empties
                # the input as it opens, and would take an answer sent sooner.
                conn.recv(64)
                conn.sendall(self.REPLY)
                sent.set()
                done.wait(5)

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        transport = SerialTransport("socket://127.0.0.1:%d" % listener.getsockname()[1])
        try:
            transport.ensure_open()
            transport.write(b"QUESTION")
            assert sent.wait(5)
            # Let the whole reply land before the one read that must take it all.
            time.sleep(0.2)

            assert transport.read_available() == self.REPLY
            assert transport.read_available() == b""
        finally:
            done.set()
            transport.close()
            listener.close()
            server.join(5)

    def test_a_link_that_never_stops_delivering_still_hands_back(self):
        # Without the cap this read would never return, and the controller
        # would never reach its deadline.
        class Flooding:
            is_open = True
            in_waiting = 1

            def read(self, size: int) -> bytes:
                return b"\x00" * size

            def close(self) -> None:
                pass

        transport = SerialTransport("socket://sign.example:4001")
        transport._port = Flooding()  # type: ignore[assignment]

        assert len(transport.read_available()) == READ_LIMIT_BYTES


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

    def test_a_long_outage_does_not_break_the_backoff(self):
        """The delay must survive far more failures than a weekend can produce.

        It did not. The delay was recomputed as ``initial * 2 ** (failures - 1)``
        from an unbounded counter, and at attempt 1025 that is ``1.0 * 2 ** 1024``,
        which raises OverflowError rather than returning infinity because the base
        is a float. An ArithmeticError is not what any caller here was catching, so
        it escaped ``ensure_open`` and killed the reconnect task, which this branch
        made the only thing able to reopen the link. The service then answered 503
        until it was restarted, however healthy the sign became.

        At the sixty second cap, attempt 1025 is about seventeen hours away: one
        weekend with the sign or its adapter switched off.
        """
        clock = Clock()
        transport = self.unopenable(clock)

        for _ in range(1200):
            with pytest.raises(TransportError):
                transport.ensure_open()
            clock.advance(transport.seconds_until_retry())

        # Still capped, still finite, still telling the truth about the wait.
        assert transport.seconds_until_retry() == 0.0
        with pytest.raises(TransportError, match="could not open"):
            transport.ensure_open()
        assert transport.seconds_until_retry() == pytest.approx(8.0)

    def test_a_successful_open_clears_the_backoff(self):
        clock = Clock()
        transport = SerialTransport("loop://", monotonic=clock)
        try:
            transport.ensure_open()
            assert transport.seconds_until_retry() == 0.0
            assert transport.last_error is None
        finally:
            transport.close()
class LinkThatDropsAtTheLock(SerialTransport):
    """A link whose port vanishes between the ``is_open`` check and the lock.

    ``write`` reads ``is_open`` outside the lock on purpose, so that a write is
    never made to wait out the reconnect loop's connection attempt. The cost of
    that is a real gap: the loop can close the port after the check has passed.
    Nothing else reproduces the gap on demand, so it is opened here by hand.
    """

    drop_at_the_lock = False

    @property
    def is_open(self) -> bool:
        if self.drop_at_the_lock:
            return True
        return super().is_open


class TestWhatADownLinkReports:
    """Every path that reports a down link reports the same thing.

    The text is the whole of what a caller gets. ``readerboard/api/errors.py``
    maps a TransportError to a 503 and uses its message as the body, so a path
    that says only "is down" leaves the caller without the reason or the wait
    that the other paths hand over.
    """

    def test_the_race_at_the_lock_says_as_much_as_the_check_before_it(self):
        clock = Clock()
        transport = LinkThatDropsAtTheLock(
            "nosuchscheme://sign", backoff_initial=4.0, monotonic=clock
        )

        # Fail an open first, so there is a reason and a window to report at all.
        with pytest.raises(TransportError, match="could not open"):
            transport.ensure_open()

        with pytest.raises(TransportError) as checked:
            transport.write(b"HELLO")

        transport.drop_at_the_lock = True
        with pytest.raises(TransportError) as raced:
            transport.write(b"HELLO")

        assert str(raced.value) == str(checked.value)
        assert "nosuchscheme://sign" in str(raced.value)
        assert "next attempt in 4.0s" in str(raced.value)

    def test_it_names_the_reason_and_the_wait(self):
        clock = Clock()
        transport = SerialTransport(
            "nosuchscheme://sign", backoff_initial=4.0, monotonic=clock
        )

        with pytest.raises(TransportError, match="could not open"):
            transport.ensure_open()
        clock.advance(1.0)

        with pytest.raises(TransportError) as down:
            transport.write(b"HELLO")

        # The wait counts down rather than being restated as the whole window.
        assert "next attempt in 3.0s" in str(down.value)
        assert transport.last_error is not None
        assert transport.last_error in str(down.value)
