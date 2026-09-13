"""Build the byte strings the sign expects.

Everything in this module is pure: bytes in, bytes out, no input or output. That
is what makes the golden byte tests in ``tests/test_frames.py`` worth having,
because they pin the wire format against the protocol document without needing a
sign to be plugged in.

A complete transmission looks like this:

    WAKEUP  SOH  sign type  address  STX  payload  EOT

The payload is one command. ``A`` writes a TEXT file, ``G`` writes a STRING file
and ``E`` writes a special function; the special function's own label follows
immediately after the ``E``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from readerboard.protocol import constants as c


class ProtocolError(ValueError):
    """A frame could not be built as asked."""


@dataclass(frozen=True, slots=True)
class FileAllocation:
    """One entry in a memory configuration.

    ``capacity`` is the file's size in bytes. ``locked`` decides whether the
    sign's own infrared keyboard may edit the file; leaving it unlocked is the
    friendlier default for a sign hanging on a wall. A STRING file has no such
    choice, and :meth:`string` builds one with the fields Table 15 requires.
    """

    label: bytes
    capacity: int
    file_type: bytes = c.FILE_TYPE_TEXT
    locked: bool = False
    schedule: bytes = c.TEXT_SCHEDULE_ALWAYS

    @classmethod
    def string(cls, label: bytes, capacity: int) -> FileAllocation:
        """Describe a STRING file: locked, and scheduled with the four zeros Table 15 asks for."""
        return cls(
            label,
            capacity,
            file_type=c.FILE_TYPE_STRING,
            locked=True,
            schedule=c.STRING_SCHEDULE,
        )

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
        if self.file_type == c.FILE_TYPE_STRING:
            _check_string_label(self.label)
            if not self.locked:
                raise ProtocolError('a STRING file must be locked; Table 15: "L" must be selected')
            if self.schedule != c.STRING_SCHEDULE:
                raise ProtocolError(
                    "a STRING file's schedule is the placeholder %r, got %r"
                    % (c.STRING_SCHEDULE, self.schedule)
                )
            if self.capacity > c.STRING_FILE_CAPACITY:
                raise ProtocolError(
                    "a STRING file holds at most %d bytes, got %d"
                    % (c.STRING_FILE_CAPACITY, self.capacity)
                )

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


def _check_string_label(label: bytes) -> None:
    """Refuse a label Appendix A rules out for a STRING file."""
    if len(label) != 1:
        raise ProtocolError("a file label is exactly one byte, got %r" % label)
    if label in c.STRING_FILE_FORBIDDEN_LABELS:
        raise ProtocolError("%r cannot be a STRING file's label" % label.decode("ascii"))


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
) -> bytes:
    """Build the payload that writes ``body`` into the TEXT file ``label``.

    An empty ``body`` blanks an ordinary file. It is not how the priority file
    is released, though: a write carrying the formatting bytes below with no
    text is read by the sign as an empty priority message that takes the screen
    over, not as a release. :func:`clear_priority_file` sends the bare write the
    release actually needs.

    The position byte is always ``TEXT_POS_MIDDLE`` and is not a parameter. All
    four of the positions the document offers draw identically on a display one
    line high, measured on the sign, so choosing between them was a choice with
    one outcome. The byte is still sent because the document requires it:
    "Display Position is irrelevant, but it still must be included."
    """
    if len(label) != 1:
        raise ProtocolError("a file label is exactly one byte, got %r" % label)
    if label == c.FILE_PRIORITY and len(body) > c.PRIORITY_FILE_CAPACITY:
        raise ProtocolError(
            "the priority file holds %d bytes and the sign will not let that change; "
            "this message needs %d" % (c.PRIORITY_FILE_CAPACITY, len(body))
        )
    return c.COMMAND_WRITE_TEXT + label + c.SOM + c.TEXT_POS_MIDDLE + mode + body


def write_string_file(label: bytes, data: bytes) -> bytes:
    """Build the payload that writes ``data`` into the STRING file ``label``.

    Unlike a TEXT write, this does not blank the display: a TEXT file that calls
    the STRING shows the new value the next time it is drawn, which in a
    scrolling mode is the next pass. An empty ``data`` empties the STRING, and a
    call to an empty STRING draws nothing at all.

    ``data`` is checked against the document's 125 byte ceiling here, and has to
    fit the STRING's own allocation as well, which only the caller knows. The
    sign does not truncate a value that is too long. Measured on 2026-09-10, it
    emptied the STRING instead, losing the previous value with it.
    """
    _check_string_label(label)
    if len(data) > c.STRING_FILE_CAPACITY:
        raise ProtocolError(
            "a STRING file holds at most %d bytes; this value needs %d"
            % (c.STRING_FILE_CAPACITY, len(data))
        )
    return c.COMMAND_WRITE_STRING + label + data


def clear_priority_file() -> bytes:
    """Build the payload that releases a priority takeover.

    The release is a write to file ``0`` "without any ASCII Message", and it has
    to be a bare one: the write command and the file label, nothing after. The
    Start-of-Message byte and the position and mode an ordinary text write
    carries are enough to make the sign read this as a priority message that
    happens to be empty, and it then takes the screen over showing nothing
    instead of releasing it. Measured on a BetaBrite Classic on 2026-09-09: the
    bare form brought the rotation straight back, the ``A0`` with formatting and
    an empty body blanked it. This was the bug behind a sign that showed alerts
    and never showed a slot, because the service clears the priority file on
    every start. See docs/protocol-notes.md.

    There is no separate release command; this degenerate write is it.
    """
    return c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY


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
    """Bytes of the sign's memory pool a configuration would take."""
    return memory_claimed_by(entry.capacity for entry in allocations)


def memory_claimed_by(sizes: Iterable[int]) -> int:
    """Bytes of the sign's memory pool files of these sizes would take.

    The figures are what the sign was measured charging rather than what the
    document says it charges: thirteen bytes of directory overhead per file
    rather than eleven, and six bytes once over the whole configuration. See
    ``MEASURED_FILE_OVERHEAD_BYTES`` in readerboard.protocol.constants for what
    separates the two, and docs/protocol-notes.md for the readings.

    This takes sizes rather than allocations so that the settings can ask what
    a pool would cost before there is a layout to build it from.
    """
    return (
        sum(size + c.MEASURED_FILE_OVERHEAD_BYTES for size in sizes)
        + c.MEASURED_POOL_OVERHEAD_BYTES
    )


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
    return write_special(
        c.CMD_SET_TIME_FORMAT,
        c.TIME_FORMAT_24_HOUR if military else c.TIME_FORMAT_12_HOUR,
    )


def set_speaker(enabled: bool) -> bytes:
    """Build the payload that enables or disables the sign's speaker.

    Disabling is a mute: the sign keeps accepting tone commands and makes no
    sound. Deliberately separate from :func:`sound_tone` and
    :func:`sound_beeps`, which never touch this register, because a sound
    command that quietly re-enabled the speaker would make the mute useless.
    """
    return write_special(c.CMD_SPEAKER_ENABLE, c.SPEAKER_ON if enabled else c.SPEAKER_OFF)


def sound_tone() -> bytes:
    """Build the payload for one continuous tone, about two seconds long."""
    return write_special(c.CMD_SPEAKER_TONE, c.TONE_CONTINUOUS)


def sound_beeps() -> bytes:
    """Build the payload for three short beeps, about two seconds in total.

    Three is the protocol's own count and is not adjustable in this form.
    """
    return write_special(c.CMD_SPEAKER_TONE, c.TONE_BEEPS)


def soft_reset() -> bytes:
    """Build the payload that puts the sign through its power-up diagnostics.

    Carries no data, which the protocol is explicit about. This is the
    non-destructive reset: the sign restarts and its memory survives, so it is
    the gentle first move on a sign whose decoder has wedged. Not to be confused
    with :func:`clear_memory`, which resets the sign by erasing it.
    """
    return write_special(c.CMD_SOFT_RESET)


# ===========================================================================
# Reading state back from the sign.
#
# These exist so that divergence between what the service believes and what the
# sign actually holds can be detected rather than assumed. This sign does answer
# them through the Ethernet to RS-232 adapter: all four came back on 2026-09-09
# and again on 2026-09-11. Nothing in the service depends on that yet, so it
# still reconciles by re-pushing on a timer, which needs no reply.
# ===========================================================================


def read_special(label: bytes) -> bytes:
    """Build the payload that asks the sign for a special function's value."""
    if len(label) != 1:
        raise ProtocolError("a special function label is exactly one byte, got %r" % label)
    return c.COMMAND_READ_SPECIAL + label


def read_general_information() -> bytes:
    """Ask the sign what it is and how it is doing.

    One read for the firmware version and release date, the sign's clock and
    time format, whether its speaker is enabled, and how much of the memory pool
    is free. :func:`readerboard.protocol.replies.parse_general_information`
    turns the answer into fields.
    """
    return read_special(c.SF_GENERAL_INFORMATION)


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


def read_string_file(label: bytes) -> bytes:
    """Ask the sign what the STRING file ``label`` holds.

    The sign answers with the write command ``G``, the label and the data, as
    it does for special function reads. Nothing in the service sends this: the
    display blanks briefly while the sign answers, and a label that was never
    allocated answers with no data, which leaves nothing to tell it from an
    empty STRING.
    """
    _check_string_label(label)
    return c.COMMAND_READ_STRING + label


def read_run_time_table() -> bytes:
    """Ask the sign for its run time table.

    The reply also carries whether a priority message is running, which is the
    only way to ask the sign whether an alert is still up.
    """
    return read_special(c.SF_RUN_TIME_TABLE)
