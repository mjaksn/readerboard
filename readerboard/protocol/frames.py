"""Build the byte strings the sign expects.

Everything in this module is pure: bytes in, bytes out, no input or output. That
is what makes the golden byte tests in ``tests/test_frames.py`` worth having,
because they pin the wire format against the protocol document without needing a
sign to be plugged in.

A complete transmission looks like this:

    WAKEUP  SOH  sign type  address  STX  payload  EOT

The payload is one command. ``A`` writes a TEXT file and ``E`` writes a special
function; the special function's own label follows immediately after the ``E``.
"""

from __future__ import annotations

from dataclasses import dataclass

from readerboard.protocol import constants as c


class ProtocolError(ValueError):
    """A frame could not be built as asked."""


@dataclass(frozen=True, slots=True)
class FileAllocation:
    """One entry in a memory configuration.

    ``capacity`` is the file's size in bytes. ``locked`` decides whether the
    sign's own infrared keyboard may edit the file; leaving it unlocked is the
    friendlier default for a sign hanging on a wall.
    """

    label: bytes
    capacity: int
    file_type: bytes = c.FILE_TYPE_TEXT
    locked: bool = False
    schedule: bytes = c.TEXT_SCHEDULE_ALWAYS

    def __post_init__(self) -> None:
        """Reject an allocation the sign could not accept."""
        if len(self.label) != 1:
            raise ProtocolError("a file label is exactly one byte, got %r" % self.label)
        if self.label == c.FILE_PRIORITY:
            raise ProtocolError(
                "the priority file '0' is allocated by the sign and must not appear "
                "in a memory configuration"
            )
        if not 0 < self.capacity <= 0xFFFF:
            raise ProtocolError("file capacity must be between 1 and 65535, got %d" % self.capacity)
        if len(self.schedule) != 4:
            raise ProtocolError("a file schedule is exactly four bytes, got %r" % self.schedule)

    def encode(self) -> bytes:
        """Render this entry as its eleven protocol characters."""
        lock = c.FILE_LOCKED if self.locked else c.FILE_UNLOCKED
        return b"%s%s%s%04X%s" % (
            self.label,
            self.file_type,
            lock,
            self.capacity,
            self.schedule,
        )


def packet(
    payload: bytes,
    *,
    sign_type: bytes = c.SIGN_TYPE_BETABRITE,
    address: bytes = c.SIGN_ADDRESS_BROADCAST,
) -> bytes:
    """Wrap a payload in the framing bytes and return the whole transmission."""
    return c.WAKEUP + c.SOH + sign_type + address + c.STX + payload + c.EOT


def write_text_file(
    label: bytes,
    body: bytes,
    *,
    mode: bytes = c.MODE_HOLD,
    position: bytes = c.TEXT_POS_MIDDLE,
) -> bytes:
    """Build the payload that writes ``body`` into the TEXT file ``label``.

    An empty ``body`` blanks the file. Writing an empty body to the priority
    file is how an alert is released, so it is explicitly allowed.
    """
    if len(label) != 1:
        raise ProtocolError("a file label is exactly one byte, got %r" % label)
    if label == c.FILE_PRIORITY and len(body) > c.PRIORITY_FILE_CAPACITY:
        raise ProtocolError(
            "the priority file holds %d bytes and the sign will not let that change; "
            "this message needs %d" % (c.PRIORITY_FILE_CAPACITY, len(body))
        )
    return c.COMMAND_WRITE_TEXT + label + c.SOM + position + mode + body


def clear_priority_file() -> bytes:
    """Build the payload that releases a priority takeover.

    Writing nothing to file ``0`` is what tells the sign to go back to playing
    its run sequence. There is no separate release command.
    """
    return write_text_file(c.FILE_PRIORITY, b"")


def write_special(label: bytes, parameter: bytes = b"") -> bytes:
    """Build the payload for a special function."""
    if len(label) != 1:
        raise ProtocolError("a special function label is exactly one byte, got %r" % label)
    return c.COMMAND_WRITE_SPECIAL + label + parameter


def clear_memory() -> bytes:
    """Build the payload that clears the sign's memory outright.

    The protocol spells this "E$" with nothing after it. It is separate from
    :func:`set_memory_config` on purpose, so that an accidentally empty list of
    allocations cannot wipe the sign by mistake.
    """
    return write_special(c.SF_SET_MEMORY_CONFIG)


def memory_claimed(allocations: list[FileAllocation]) -> int:
    """Bytes of the sign's memory pool a configuration would take.

    The protocol charges each configured file eleven bytes of directory
    overhead on top of its own size, and the total has to fit the pool.
    """
    return sum(entry.capacity + c.FILE_OVERHEAD_BYTES for entry in allocations)


def set_memory_config(allocations: list[FileAllocation]) -> bytes:
    """Build the payload that allocates the sign's files.

    This erases every file already on the sign, so the whole pool has to be
    described in one call. Note also that, apart from the priority file and the
    default file ``A``, no file can be written at all until a memory
    configuration has been written. See docs/protocol-notes.md.
    """
    if not allocations:
        raise ProtocolError(
            "a memory configuration needs at least one file; use clear_memory() to "
            "deliberately wipe the sign"
        )

    labels = [entry.label for entry in allocations]
    if len(set(labels)) != len(labels):
        raise ProtocolError("a memory configuration cannot name the same file twice")

    body = b"".join(entry.encode() for entry in allocations)
    return write_special(c.SF_SET_MEMORY_CONFIG, body)


def set_run_sequence(
    labels: list[bytes],
    *,
    mode: bytes = c.RUN_SEQ_IGNORE_TIME,
    locked: bool = False,
) -> bytes:
    """Build the payload that decides which TEXT files play, and in what order.

    An empty ``labels`` is meaningful: it stops the sign playing anything from
    the pool, which is what an emptied registry should look like.

    The default mode ignores each file's own start and stop time. The service
    allocates every file as always eligible, so the two modes behave the same
    for it, but being explicit means a file that later gains a schedule does not
    silently change how the rotation behaves.
    """
    for label in labels:
        if len(label) != 1:
            raise ProtocolError("a file label is exactly one byte, got %r" % label)
    if len(set(labels)) != len(labels):
        raise ProtocolError("a run sequence cannot name the same file twice")

    parameter = mode + (c.FILE_LOCKED if locked else c.FILE_UNLOCKED) + b"".join(labels)
    return write_special(c.SF_SET_RUN_SEQUENCE, parameter)


def set_time(hour: int, minute: int) -> bytes:
    """Build the payload that sets the sign's clock, as HHMM on a 24 hour clock."""
    if not 0 <= hour <= 23:
        raise ProtocolError("hour must be between 0 and 23, got %d" % hour)
    if not 0 <= minute <= 59:
        raise ProtocolError("minute must be between 0 and 59, got %d" % minute)
    return write_special(c.CMD_SET_TIME, b"%02d%02d" % (hour, minute))


def set_day_of_week(day: int) -> bytes:
    """Build the payload that sets the sign's day of week, 1 for Sunday to 7 for Saturday."""
    if not 1 <= day <= 7:
        raise ProtocolError("day of week must be between 1 and 7, got %d" % day)
    return write_special(c.CMD_SET_DAY_OF_WEEK, b"%d" % day)


def set_time_format(military: bool) -> bytes:
    """Build the payload that chooses a 24 hour or 12 hour clock on the sign."""
    return write_special(c.CMD_SET_TIME_FORMAT, b"M" if military else b"S")


# ===========================================================================
# Reading state back from the sign.
#
# These exist so that divergence between what the service believes and what the
# sign actually holds can be detected rather than assumed. Whether this sign
# answers them through the Ethernet to RS-232 adapter is the one genuinely
# unproven thing left; two-way traffic over that path has never been tried.
# scripts/protocol_spike.py settles it. Until then the service reconciles by
# re-pushing on a timer, which needs no reply.
# ===========================================================================


def read_special(label: bytes) -> bytes:
    """Build the payload that asks the sign for a special function's value."""
    if len(label) != 1:
        raise ProtocolError("a special function label is exactly one byte, got %r" % label)
    return c.COMMAND_READ_SPECIAL + label


def read_memory_config() -> bytes:
    """Ask the sign for its memory configuration table.

    Note when comparing the reply against a plan: the sign hands whatever is
    left of the memory pool to the first file in the configuration once it
    starts running, so the first file's size will not match what was sent.
    Compare the plan semantically, not byte for byte.
    """
    return read_special(c.SF_SET_MEMORY_CONFIG)


def read_memory_pool_size() -> bytes:
    """Ask the sign for the total and unused size of its memory pool."""
    return read_special(c.SF_MEMORY_POOL_SIZE)


def read_run_sequence() -> bytes:
    """Ask the sign which files it is currently playing, and in what order."""
    return read_special(c.SF_SET_RUN_SEQUENCE)


def read_run_time_table() -> bytes:
    """Ask the sign for its run time table.

    The reply also carries whether a priority message is running, which is the
    only way to ask the sign whether an alert is still up.
    """
    return read_special(c.SF_RUN_TIME_TABLE)
