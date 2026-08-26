"""A transport that keeps the packets instead of sending them.

This is what the tests assert against. It is not a mock of the serial link so
much as a sign that writes everything down: tests read ``packets`` to see
exactly what would have gone down the wire, which is the same thing the golden
byte tests in ``tests/test_frames.py`` check the builders produce.

It can also be told to fail, which is how the reconnect and error paths get
exercised without unplugging anything.
"""

from __future__ import annotations

from readerboard.transport.base import TransportError


class FakeTransport:
    """An in-memory transport that records every packet written to it."""

    def __init__(self, *, fail_with: str | None = None, open_fails_with: str | None = None) -> None:
        """Create a transport. Pass a reason to make writes or opens fail."""
        self.packets: list[bytes] = []
        self.fail_with = fail_with
        self.open_fails_with = open_fails_with
        self.open_count = 0
        self.close_count = 0
        self.write_count = 0
        self._is_open = False

    @property
    def is_open(self) -> bool:
        """Whether the link is currently up."""
        return self._is_open

    @property
    def description(self) -> str:
        """A short name for the link, safe to log."""
        return "fake://"

    @property
    def last_error(self) -> str | None:
        """Why the link last failed, or None."""
        return self.open_fails_with or self.fail_with

    def seconds_until_retry(self) -> float:
        """How long until a reconnect would be attempted. The fake never waits."""
        return 0.0

    def ensure_open(self) -> None:
        """Open the link if it is down."""
        if self._is_open:
            return
        if self.open_fails_with is not None:
            raise TransportError(self.open_fails_with)
        self._is_open = True
        self.open_count += 1

    def write(self, data: bytes) -> None:
        """Record one transmission, or fail if the fake was told to."""
        self.ensure_open()
        if self.fail_with is not None:
            self._is_open = False
            raise TransportError(self.fail_with)
        self.write_count += 1
        self.packets.append(data)

    def close(self) -> None:
        """Close the link."""
        if self._is_open:
            self.close_count += 1
        self._is_open = False

    # == helpers for tests ==================================================

    @property
    def last_packet(self) -> bytes:
        """The most recent packet written, for the common single-write assertion."""
        if not self.packets:
            raise AssertionError("nothing has been written to this transport")
        return self.packets[-1]

    def clear(self) -> None:
        """Forget every recorded packet, leaving the counters alone."""
        self.packets.clear()
