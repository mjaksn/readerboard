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

# The largest frame worth buffering. The sign's whole memory pool is around
# 30000 bytes and a single transmission writes a fraction of that, so anything
# past this is not a frame that lost its EOT, it is a peer that will never send
# one. Without a ceiling the buffer grows for as long as such a peer keeps
# talking.
MAX_FRAME_BYTES = 65536

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

    def reset(self) -> None:
        """Throw away a part-read frame and start again at the next header.

        Needed whenever bytes have been dropped rather than fed in. Left alone,
        the prefix of the half frame already buffered would be joined to
        whatever arrives next and reported as a corrupt transmission that
        nobody sent.
        """
        self._buffer.clear()
        self._reset_preamble()

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
        restart, waiting = self._next_header(end)

        if waiting:
            # A candidate header this close to the end of the buffer cannot be
            # judged yet. Wait rather than guess, since guessing wrong either
            # cuts a good frame short or swallows the next one.
            return None

        truncated = restart >= 0 and (end < 0 or restart < end)
        if truncated:
            # A second header arrived before this frame was terminated, so this
            # one will never be. Emit what there is and resync at the new header.
            frame = bytes(self._buffer[:restart])
            del self._buffer[:restart]
        elif end < 0:
            return self._overlong()
        else:
            frame = bytes(self._buffer[: end + 1])
            del self._buffer[: end + 1]

        payload_end = len(frame) - 1 if not truncated else len(frame)
        transmission = self._build(frame, frame[_HEADER_LENGTH:payload_end], truncated)
        self._reset_preamble()
        return transmission

    def _next_header(self, end: int) -> tuple[int, bool]:
        """Find the next real header after this one, or say to wait for more.

        A bare `SOH` is not a header. The payload of a DOTS picture is binary
        and may contain 0x01 anywhere, so treating every one as the start of a
        new transmission cuts good frames in half and loses the rest of them.
        A header is only a header when an `STX` follows four bytes later, which
        is the same test :meth:`_take_one` applies.

        Returns the offset of the next header and whether the answer has to
        wait for more bytes. Only candidates before ``end`` matter: past the
        `EOT` this frame is finished and the next one is somebody else's
        problem.

        Waiting is only ever right while this frame is unterminated. Once an
        `EOT` has been seen the frame is complete, and a candidate too close to
        that `EOT` to carry its own `STX` cannot be a header, because a header's
        sign type and address bytes are printable and can never be the 0x04
        that was found. More data would not change that, so deciding now is
        safe and waiting would deadlock a frame that has already arrived whole.
        """
        terminated = end >= 0
        limit = len(self._buffer) if not terminated else end
        at = _HEADER_LENGTH
        while True:
            found = self._buffer.find(c.SOH, at, limit)
            if found < 0:
                return -1, False
            if len(self._buffer) - found < _HEADER_LENGTH:
                if terminated:
                    return -1, False
                # Not enough bytes yet to tell a header from a payload byte.
                return -1, True
            if self._buffer[found + 4 : found + 5] == c.STX:
                return found, False
            at = found + 1

    def _overlong(self) -> Transmission | None:
        """Give up on a frame that has run past any length a sign would accept.

        Without this the buffer grows for as long as a peer keeps sending bytes
        that are neither an `EOT` nor a header, which is reachable from the
        network whenever somebody takes the ``--host`` option. Everything else
        malformed here ends as a transmission with a complaint on it, so this
        does too rather than by silently dropping the connection's data.
        """
        if len(self._buffer) <= MAX_FRAME_BYTES:
            return None

        frame = bytes(self._buffer)
        self._buffer.clear()
        transmission = self._build(frame, frame[_HEADER_LENGTH:], truncated=True)
        self._reset_preamble()
        return transmission

    def _reset_preamble(self) -> None:
        """Forget the nulls and junk counted for the frame just emitted."""
        self._nulls = 0
        self._junk.clear()
        self._junk_count = 0

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
        if truncated and len(frame) > MAX_FRAME_BYTES:
            complaints.append(
                "no EOT after %d bytes, which is past anything the sign has room "
                "for. The scanner gave up on this frame and will resynchronise at "
                "the next header" % len(frame)
            )
        elif truncated:
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
