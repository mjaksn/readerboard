"""Pull whole transmissions out of a byte stream that arrives in pieces.

The sign is at the end of a TCP socket, so nothing guarantees that one write by
the service is one read here. A transmission can arrive split across three
reads, two transmissions can arrive in one, and a client that connects halfway
through a write hands over the tail of a frame that never had a header. All
three happen, and none of them should look like a protocol error to whoever is
watching the log.

So this is a scanner rather than a parser. It buffers, finds the next `SOH`,
checks the header, reads to the `EOT`, and hands back what it found along with
whatever it had to complain about on the way. It never raises: a stream it
cannot make sense of produces complaints attached to the next transmission, not
an exception that would take the window down.

Nothing here imports Qt, and nothing here holds state beyond the stream it is
reading, which is what lets the tests run in CI where PySide6 is not installed.
"""

from __future__ import annotations

from dataclasses import dataclass

from readerboard.protocol import constants as c

# The document specifies five nulls, describing them as what "cause a sign to
# lock onto a baud rate". The service sends six. Fewer than five is worth
# mentioning; more is not, because more than required is harmless.
EXPECTED_WAKEUP_NULLS = 5

# Junk before a header is kept so it can be shown, but a stream of noise should
# not grow the buffer without bound. Past this, the count keeps rising and the
# bytes stop being stored.
MAX_STORED_JUNK = 256

# SOH, one sign type byte, two address bytes, STX.
_HEADER_LENGTH = 5


@dataclass(frozen=True, slots=True)
class Transmission:
    """One complete `SOH` to `EOT` frame, with what preceded it.

    ``raw`` is the frame itself without the wakeup nulls, so it begins with
    `SOH` and ends with `EOT`. ``complaints`` is everything the scanner noticed
    that a sign would either ignore or quietly fail on, which is exactly the
    kind of thing worth putting in front of somebody debugging.
    """

    raw: bytes
    wakeup_nulls: int
    sign_type: bytes
    address: bytes
    payload: bytes
    junk_before: bytes
    junk_before_count: int
    complaints: tuple[str, ...]

    @property
    def is_truncated(self) -> bool:
        """Whether the frame ended at a new header rather than at an `EOT`."""
        return not self.raw.endswith(c.EOT)


class FrameScanner:
    """Turns a stream of bytes into transmissions, one connection's worth.

    Give each connection its own scanner. Two clients sharing one would
    interleave their bytes into frames that neither of them sent.
    """

    def __init__(self) -> None:
        """Create an empty scanner, ready for the first read."""
        self._buffer = bytearray()
        self._nulls = 0
        self._junk = bytearray()
        self._junk_count = 0

    @property
    def pending_bytes(self) -> int:
        """How many buffered bytes are not yet part of a complete transmission."""
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Transmission]:
        """Add bytes from the wire and return every transmission they completed."""
        self._buffer += data
        found: list[Transmission] = []

        while True:
            transmission = self._take_one()
            if transmission is None:
                return found
            found.append(transmission)

    # == internals ==========================================================

    def _take_one(self) -> Transmission | None:
        """Consume and return the next whole transmission, or None if incomplete."""
        while True:
            start = self._buffer.find(c.SOH)
            if start < 0:
                # Nothing here is a header yet, so all of it is preamble. The
                # buffer is emptied rather than held, because holding it would
                # mean re-scanning the same noise on every later read.
                self._absorb_preamble(self._buffer)
                self._buffer.clear()
                return None

            self._absorb_preamble(self._buffer[:start])
            del self._buffer[:start]

            if len(self._buffer) < _HEADER_LENGTH:
                return None

            if self._buffer[4:5] != c.STX:
                # A 0x01 that is not the start of a header. Drop it and keep
                # looking rather than treating the rest of the stream as lost.
                self._absorb_preamble(self._buffer[:1])
                del self._buffer[:1]
                continue

            return self._read_frame()

    def _read_frame(self) -> Transmission | None:
        """Read from a validated header to the `EOT`, or to the next header."""
        end = self._buffer.find(c.EOT, _HEADER_LENGTH)
        restart = self._buffer.find(c.SOH, _HEADER_LENGTH)

        truncated = restart >= 0 and (end < 0 or restart < end)
        if truncated:
            # A second header arrived before this frame was terminated, so this
            # one will never be. Emit what there is and resync at the new header.
            frame = bytes(self._buffer[:restart])
            del self._buffer[:restart]
        elif end < 0:
            return None
        else:
            frame = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]

        payload_end = len(frame) - 1 if not truncated else len(frame)
        transmission = self._build(frame, frame[_HEADER_LENGTH:payload_end], truncated)
        self._nulls = 0
        self._junk.clear()
        self._junk_count = 0
        return transmission

    def _build(self, frame: bytes, payload: bytes, truncated: bool) -> Transmission:
        """Assemble a transmission and work out what to complain about."""
        sign_type = frame[1:2]
        address = frame[2:4]
        complaints: list[str] = []

        if self._nulls < EXPECTED_WAKEUP_NULLS:
            complaints.append(
                "%d wakeup nulls before this transmission; the protocol asks for %d, "
                "which is what lets the sign lock onto the baud rate"
                % (self._nulls, EXPECTED_WAKEUP_NULLS)
            )
        if self._junk_count:
            complaints.append(
                "%d byte(s) of data that is neither a wakeup null nor a header "
                "preceded this transmission" % self._junk_count
            )
        if sign_type != c.SIGN_TYPE_BETABRITE:
            complaints.append(
                "sign type %r, not the BetaBrite %r; a real BetaBrite ignores a "
                "transmission addressed to another type"
                % (sign_type.decode("latin-1"), c.SIGN_TYPE_BETABRITE.decode("latin-1"))
            )
        if truncated:
            complaints.append(
                "no EOT: a new transmission began before this one ended, so the sign "
                "would have discarded it"
            )

        return Transmission(
            raw=frame,
            wakeup_nulls=self._nulls,
            sign_type=sign_type,
            address=address,
            payload=payload,
            junk_before=bytes(self._junk),
            junk_before_count=self._junk_count,
            complaints=tuple(complaints),
        )

    def _absorb_preamble(self, data: bytes | bytearray) -> None:
        """Count wakeup nulls and set aside anything else seen before a header."""
        for byte in data:
            if byte == 0:
                self._nulls += 1
                continue
            self._junk_count += 1
            if len(self._junk) < MAX_STORED_JUNK:
                self._junk.append(byte)
