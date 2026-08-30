"""Turn a transmission's payload into the command it spells, and say so in words.

A payload is one command. ``AA<SOM> bHI`` is a write of the word HI into TEXT
file A, held still and centred vertically. Reading that off a hex dump is
possible and nobody enjoys it, so this module does it once and hands back both
the structured form the sign model needs and the plain sentence a person wants.

Two tolerances are deliberate, because this decodes traffic the service did not
necessarily send:

- The start of mode is optional. The document allows a Write TEXT with no
  ``SOM`` at all, in which case the whole remainder is the message and the sign
  keeps whatever mode the file already had. The service always sends one, so a
  payload without it is worth a complaint but not a failure.
- Nothing here raises. A command that makes no sense comes back as an
  ``Unrecognised`` carrying its own complaint, because a debugging tool that
  falls over on the malformed packet is useless at the one moment it is needed.

Nothing here imports Qt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from readerboard.protocol import constants as c
from readerboard.protocol.tokens import DISPLAY_MODES, TEXT_POSITIONS
from signsim.framing import Transmission
from signsim.spans import Span, SpanKind, annotate, readable

# SOH, sign type, two address bytes, STX. The payload starts here.
_PAYLOAD_OFFSET = 5

# Each entry in a memory configuration is eleven characters: FTPSIZEQQQQ.
MEMORY_ENTRY_LENGTH = 11


@dataclass(frozen=True, slots=True)
class Detail:
    """One named thing to show in the detail pane, with an optional aside."""

    name: str
    value: str
    note: str = ""


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One file's line in a memory configuration table."""

    label: bytes
    file_type: bytes
    locked: bool
    capacity: int
    schedule: bytes

    @property
    def always_eligible(self) -> bool:
        """Whether this file may play whenever the run sequence names it.

        A start time of ``FF`` means always, and the protocol says the stop time
        is ignored entirely when the start time is Always.
        """
        return self.schedule[:2].upper() == b"FF"


@dataclass(frozen=True)
class Command:
    """What a payload turned out to be.

    Subclasses add whatever the sign model needs to apply the command. Every one
    of them carries ``summary`` for the log row, ``details`` for the pane beside
    it, ``spans`` for the coloured hex, and ``complaints`` for anything the sign
    would have shrugged off in silence.
    """

    code: bytes
    name: str
    summary: str
    details: tuple[Detail, ...] = ()
    spans: tuple[Span, ...] = ()
    complaints: tuple[str, ...] = ()


@dataclass(frozen=True)
class WriteText(Command):
    """A Write TEXT file, which is how every message reaches the sign."""

    label: bytes = b""
    position: bytes = b""
    mode: bytes = b""
    body: bytes = b""
    has_start_of_mode: bool = True


@dataclass(frozen=True)
class SetMemoryConfig(Command):
    """A Set Memory Configuration, which erases every message on the sign."""

    entries: tuple[MemoryEntry, ...] = ()


@dataclass(frozen=True)
class ClearMemory(Command):
    """``E$`` with nothing after it, which wipes the sign outright."""


@dataclass(frozen=True)
class SetRunSequence(Command):
    """A Set Run Sequence, which decides which files play and in what order."""

    sequence_mode: bytes = b""
    locked: bool = False
    labels: tuple[bytes, ...] = ()


@dataclass(frozen=True)
class SetTime(Command):
    """A set of the sign's internal clock."""

    hour: int = 0
    minute: int = 0


@dataclass(frozen=True)
class SetDayOfWeek(Command):
    """A set of the sign's day of week, 1 for Sunday through 7 for Saturday."""

    day: int = 0


@dataclass(frozen=True)
class SetTimeFormat(Command):
    """A choice between the sign's 12 hour and 24 hour clock."""

    military: bool = False


@dataclass(frozen=True)
class WriteSpecial(Command):
    """A special function this tool has no dedicated reading for."""

    label: bytes = b""
    parameter: bytes = b""


@dataclass(frozen=True)
class ReadCommand(Command):
    """A read. The sign would answer these; this tool listens and does not."""

    label: bytes = b""


@dataclass(frozen=True)
class Unrecognised(Command):
    """A payload that is not a command shape this tool knows."""

    payload: bytes = b""


@dataclass(frozen=True)
class DecodedTransmission:
    """A transmission, what it said, and the spans covering its whole frame."""

    transmission: Transmission
    command: Command
    spans: tuple[Span, ...] = field(default=())

    @property
    def complaints(self) -> tuple[str, ...]:
        """Everything worth flagging, from the framing and from the command."""
        return self.transmission.complaints + self.command.complaints


# ===========================================================================
# Lookup tables, all built from the service's own constants.
# ===========================================================================


def _sign_types() -> dict[bytes, str]:
    """Map each sign type byte to the name the constants give it."""
    found: dict[bytes, str] = {}
    for name, value in vars(c).items():
        if not name.startswith("SIGN_TYPE_") or not isinstance(value, bytes):
            continue
        found.setdefault(value, name.removeprefix("SIGN_TYPE_").lower().replace("_", " "))
    return found


SIGN_TYPES = _sign_types()

COMMAND_NAMES: dict[bytes, str] = {
    c.COMMAND_WRITE_TEXT: "Write TEXT file",
    c.COMMAND_READ_TEXT: "Read TEXT file",
    c.COMMAND_WRITE_SPECIAL: "Write SPECIAL function",
    c.COMMAND_READ_SPECIAL: "Read SPECIAL function",
    c.COMMAND_WRITE_STRING: "Write STRING file",
    c.COMMAND_READ_STRING: "Read STRING file",
    c.COMMAND_WRITE_DOTS: "Write DOTS picture",
    c.COMMAND_READ_DOTS: "Read DOTS picture",
    c.COMMAND_WRITE_ALPHA_DOTS: "Write ALPHAVISION DOTS picture",
    c.COMMAND_READ_ALPHA_DOTS: "Read ALPHAVISION DOTS picture",
    c.COMMAND_ALPHA_BULLETIN: "Write ALPHAVISION BULLETIN",
}

SPECIAL_FUNCTION_NAMES: dict[bytes, str] = {
    c.SF_SET_MEMORY_CONFIG: "memory configuration",
    c.SF_SET_RUN_SEQUENCE: "run sequence",
    c.SF_MEMORY_POOL_SIZE: "memory pool size",
    c.SF_RUN_TIME_TABLE: "run time table",
    c.CMD_SET_TIME: "time of day",
    c.CMD_SET_DAY_OF_WEEK: "day of week",
    c.CMD_SET_TIME_FORMAT: "time format",
}

FILE_TYPE_NAMES: dict[bytes, str] = {
    c.FILE_TYPE_TEXT: "TEXT",
    c.FILE_TYPE_STRING: "STRING",
    c.FILE_TYPE_DOTS: "DOTS",
}

RUN_SEQUENCE_MODES: dict[bytes, str] = {
    c.RUN_SEQ_BY_TIME: "by each file's own run times",
    c.RUN_SEQ_IGNORE_TIME: "in the given order, ignoring each file's run times",
    c.RUN_SEQ_DELETE_AT_STOP: "by run times, deleting each file at its stop time",
}

DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

_MODES_BY_VALUE = {token.value: token for token in DISPLAY_MODES}
_POSITIONS_BY_VALUE = {token.value: token for token in TEXT_POSITIONS}


def mode_name(value: bytes) -> str:
    """Name a display mode, or say it is unknown."""
    token = _MODES_BY_VALUE.get(value)
    return token.text if token is not None else "unknown mode %r" % value.decode("latin-1")


def position_name(value: bytes) -> str:
    """Name a vertical text position, or say it is unknown."""
    token = _POSITIONS_BY_VALUE.get(value)
    return token.text if token is not None else "unknown position %r" % value.decode("latin-1")


def printable(data: bytes) -> str:
    """Render bytes the way a log line should show them."""
    return data.decode("latin-1")


# ===========================================================================
# Decoding.
# ===========================================================================


def decode(transmission: Transmission) -> DecodedTransmission:
    """Decode a whole transmission, spans and all."""
    command = decode_payload(transmission.payload, offset=_PAYLOAD_OFFSET)
    return DecodedTransmission(
        transmission=transmission,
        command=command,
        spans=tuple(_frame_spans(transmission, command)),
    )


def _frame_spans(transmission: Transmission, command: Command) -> list[Span]:
    """Cover the whole frame, so the hex view can colour every byte of it."""
    raw = transmission.raw
    sign_type = transmission.sign_type
    type_name = SIGN_TYPES.get(sign_type, "an unlisted sign type")

    spans = [
        Span(SpanKind.FRAMING, 0, c.SOH, "SOH", "Start of header, which begins a transmission"),
        Span(
            SpanKind.FRAMING,
            1,
            sign_type,
            "sign type %s" % printable(sign_type),
            "Which kind of sign should listen: %s" % type_name,
        ),
        Span(
            SpanKind.FRAMING,
            2,
            transmission.address,
            "address %s" % printable(transmission.address),
            "Which sign should listen. '00' is a broadcast to all of them",
        ),
        Span(SpanKind.FRAMING, 4, c.STX, "STX", "Start of text, which begins the command"),
    ]
    spans.extend(command.spans)
    if not transmission.is_truncated:
        spans.append(
            Span(
                SpanKind.FRAMING,
                len(raw) - 1,
                c.EOT,
                "EOT",
                "End of transmission. The sign acts on what came before it",
            )
        )
    return spans


def decode_payload(payload: bytes, *, offset: int = 0) -> Command:
    """Decode one command payload, numbering its spans from ``offset``."""
    if not payload:
        return Unrecognised(
            code=b"",
            name="Empty payload",
            summary="Nothing between the STX and the EOT",
            spans=(),
            complaints=("this transmission carried no command at all",),
            payload=b"",
        )

    code = payload[0:1]
    if code == c.COMMAND_WRITE_TEXT:
        return _write_text(payload, offset)
    if code == c.COMMAND_WRITE_SPECIAL:
        return _write_special(payload, offset)
    if code in (c.COMMAND_READ_TEXT, c.COMMAND_READ_SPECIAL, c.COMMAND_READ_STRING,
                c.COMMAND_READ_DOTS, c.COMMAND_READ_ALPHA_DOTS):
        return _read(payload, offset)
    if code in COMMAND_NAMES:
        return _generic(payload, offset)

    return Unrecognised(
        code=code,
        name="Unrecognised command",
        summary="Command code %r is not one the protocol lists" % printable(code),
        spans=tuple(annotate(payload, offset=offset)),
        complaints=(
            "command code %r is not in the protocol's command table, so the sign "
            "would ignore this transmission" % printable(code),
        ),
        payload=payload,
    )


def _command_span(code: bytes, offset: int) -> Span:
    """Make the span for the leading command byte."""
    return Span(
        SpanKind.COMMAND,
        offset,
        code,
        "command %s" % printable(code),
        COMMAND_NAMES.get(code, "An unlisted command code"),
    )


def _write_text(payload: bytes, offset: int) -> Command:
    """Decode a Write TEXT file, the command that carries every message."""
    code = payload[0:1]
    label = payload[1:2]
    complaints: list[str] = []
    spans = [_command_span(code, offset)]

    if not label:
        return Unrecognised(
            code=code,
            name="Write TEXT file",
            summary="A write with no file label",
            spans=tuple(spans),
            complaints=("a Write TEXT file needs a one byte file label after the 'A'",),
            payload=payload,
        )

    is_priority = label == c.FILE_PRIORITY
    spans.append(
        Span(
            SpanKind.COMMAND,
            offset + 1,
            label,
            "file %s" % printable(label),
            "The priority TEXT file, which suppresses every other file while it holds "
            "a message" if is_priority else "The TEXT file this message is written into",
        )
    )

    rest = payload[2:]
    position = b""
    mode = b""
    has_som = rest[0:1] == c.SOM

    if has_som:
        position = rest[1:2]
        mode_length = 2 if rest[2:3] == b"n" else 1
        mode = rest[2 : 2 + mode_length]
        body = rest[2 + mode_length :]
        spans.append(
            Span(SpanKind.CONTROL, offset + 2, c.SOM, "SOM", "Start of mode, which introduces "
                 "the position and display mode for what follows")
        )
        if position:
            spans.append(
                Span(
                    SpanKind.CONTROL,
                    offset + 3,
                    position,
                    position_name(position),
                    "Where the text sits vertically on the sign",
                )
            )
        if mode:
            spans.append(
                Span(
                    SpanKind.CONTROL,
                    offset + 4,
                    mode,
                    mode_name(mode),
                    "How the message arrives on the sign",
                )
            )
        if position and position not in _POSITIONS_BY_VALUE:
            complaints.append(
                "vertical position %r is not one the protocol lists" % printable(position)
            )
        if mode and mode not in _MODES_BY_VALUE:
            complaints.append("display mode %r is not one the protocol lists" % printable(mode))
    else:
        body = rest
        complaints.append(
            "no start of mode after the file label, so the sign would take the whole "
            "remainder as the message and keep the mode the file already had"
        )

    body_offset = offset + len(payload) - len(body)
    body_spans = annotate(body, offset=body_offset)
    spans.extend(body_spans)

    if is_priority and len(body) > c.PRIORITY_FILE_CAPACITY:
        complaints.append(
            "the priority file holds %d bytes and this message is %d, so the sign "
            "would truncate it" % (c.PRIORITY_FILE_CAPACITY, len(body))
        )

    if not body:
        summary = (
            "Release the priority file, letting the run sequence resume"
            if is_priority
            else "Blank TEXT file %s" % printable(label)
        )
    else:
        summary = "Write TEXT file %s: %s" % (printable(label), readable(body_spans))

    details = [
        Detail("File", printable(label), "priority" if is_priority else ""),
        Detail("Message", readable(body_spans) or "empty"),
        Detail("Body length", "%d bytes" % len(body)),
    ]
    if has_som:
        details.insert(1, Detail("Mode", mode_name(mode), printable(mode)))
        details.insert(2, Detail("Position", position_name(position), printable(position)))

    return WriteText(
        code=code,
        name="Write TEXT file",
        summary=summary,
        details=tuple(details),
        spans=tuple(spans),
        complaints=tuple(complaints),
        label=label,
        position=position,
        mode=mode,
        body=body,
        has_start_of_mode=has_som,
    )


def _write_special(payload: bytes, offset: int) -> Command:
    """Decode a Write SPECIAL function by dispatching on its label."""
    code = payload[0:1]
    label = payload[1:2]
    parameter = payload[2:]

    if not label:
        return Unrecognised(
            code=code,
            name="Write SPECIAL function",
            summary="A special function write with no function label",
            spans=(_command_span(code, offset),),
            complaints=("a Write SPECIAL function needs a one byte label after the 'E'",),
            payload=payload,
        )

    spans = [
        _command_span(code, offset),
        Span(
            SpanKind.COMMAND,
            offset + 1,
            label,
            "function %s" % printable(label),
            "Special function: %s"
            % SPECIAL_FUNCTION_NAMES.get(label, "not one this tool has a reading for"),
        ),
    ]

    if label == c.SF_SET_MEMORY_CONFIG:
        return _memory_config(code, parameter, offset, spans)
    if label == c.SF_SET_RUN_SEQUENCE:
        return _run_sequence(code, parameter, offset, spans)
    if label == c.CMD_SET_TIME:
        return _set_time(code, parameter, offset, spans)
    if label == c.CMD_SET_DAY_OF_WEEK:
        return _set_day(code, parameter, offset, spans)
    if label == c.CMD_SET_TIME_FORMAT:
        return _set_time_format(code, parameter, offset, spans)

    spans.extend(annotate(parameter, offset=offset + 2))
    return WriteSpecial(
        code=code,
        name="Write SPECIAL function",
        summary="Special function %s with %d bytes of parameter"
        % (printable(label), len(parameter)),
        details=(
            Detail("Function", printable(label)),
            Detail("Parameter", _preview(parameter)),
        ),
        spans=tuple(spans),
        complaints=(),
        label=label,
        parameter=parameter,
    )


def _memory_config(code: bytes, parameter: bytes, offset: int, spans: list[Span]) -> Command:
    """Decode ``E$``, which is either a whole file table or a wipe."""
    if not parameter:
        return ClearMemory(
            code=code,
            name="Clear memory",
            summary="Clear memory: every file on the sign is erased",
            details=(Detail("Effect", "The sign keeps no files at all after this"),),
            spans=tuple(spans),
            complaints=(),
        )

    complaints: list[str] = []
    entries: list[MemoryEntry] = []
    at = offset + 2
    remainder = parameter

    while len(remainder) >= MEMORY_ENTRY_LENGTH:
        chunk = remainder[:MEMORY_ENTRY_LENGTH]
        entry, chunk_complaints = _memory_entry(chunk)
        complaints.extend(chunk_complaints)
        if entry is not None:
            entries.append(entry)
            spans.append(
                Span(
                    SpanKind.COMMAND,
                    at,
                    chunk,
                    "file %s" % printable(entry.label),
                    "%s file, %s, %d bytes, %s"
                    % (
                        FILE_TYPE_NAMES.get(entry.file_type, "unknown type"),
                        "locked against the infrared keyboard"
                        if entry.locked
                        else "editable from the infrared keyboard",
                        entry.capacity,
                        "always eligible to play"
                        if entry.always_eligible
                        else "scheduled %s" % printable(entry.schedule),
                    ),
                )
            )
        else:
            spans.append(
                Span(SpanKind.UNKNOWN, at, chunk, "malformed entry", "Not a valid FTPSIZEQQQQ group")
            )
        remainder = remainder[MEMORY_ENTRY_LENGTH:]
        at += MEMORY_ENTRY_LENGTH

    if remainder:
        complaints.append(
            "%d byte(s) left over after the last complete eleven character entry; a "
            "memory configuration is a whole number of FTPSIZEQQQQ groups" % len(remainder)
        )
        spans.append(
            Span(SpanKind.UNKNOWN, at, remainder, "trailing bytes", "Not a whole entry")
        )

    claimed = sum(entry.capacity + c.FILE_OVERHEAD_BYTES for entry in entries)
    return SetMemoryConfig(
        code=code,
        name="Set memory configuration",
        summary="Set memory configuration: %d file(s), %d bytes claimed. This erases "
        "everything already on the sign" % (len(entries), claimed),
        details=(
            Detail("Files", "%d" % len(entries)),
            Detail(
                "Memory claimed",
                "%d bytes" % claimed,
                "each file's own size plus %d bytes of directory overhead"
                % c.FILE_OVERHEAD_BYTES,
            ),
            Detail(
                "Labels",
                " ".join(printable(entry.label) for entry in entries) or "none",
            ),
        ),
        spans=tuple(spans),
        complaints=tuple(complaints),
        entries=tuple(entries),
    )


def _memory_entry(chunk: bytes) -> tuple[MemoryEntry | None, list[str]]:
    """Read one eleven character FTPSIZEQQQQ group."""
    complaints: list[str] = []
    label = chunk[0:1]
    file_type = chunk[1:2]
    lock = chunk[2:3]
    size = chunk[3:7]
    schedule = chunk[7:11]

    try:
        capacity = int(size, 16)
    except ValueError:
        return None, ["file %r has a size field %r that is not hexadecimal"
                      % (printable(label), printable(size))]

    if file_type not in FILE_TYPE_NAMES:
        complaints.append(
            "file %r has type %r, which the protocol does not list"
            % (printable(label), printable(file_type))
        )
    if lock not in (c.FILE_LOCKED, c.FILE_UNLOCKED):
        complaints.append(
            "file %r has lock flag %r, which is neither 'L' nor 'U'"
            % (printable(label), printable(lock))
        )
    if label == c.FILE_PRIORITY:
        complaints.append(
            "the priority file '0' always exists and sits outside the memory pool, so "
            "it must not appear in a memory configuration"
        )

    return (
        MemoryEntry(
            label=label,
            file_type=file_type,
            locked=lock == c.FILE_LOCKED,
            capacity=capacity,
            schedule=schedule,
        ),
        complaints,
    )


def _run_sequence(code: bytes, parameter: bytes, offset: int, spans: list[Span]) -> Command:
    """Decode ``E.``, whose parameter is a mode, a lock flag and the file labels."""
    complaints: list[str] = []
    sequence_mode = parameter[0:1]
    lock = parameter[1:2]
    labels = tuple(parameter[index : index + 1] for index in range(2, len(parameter)))

    if len(parameter) < 2:
        complaints.append(
            "a run sequence needs at least a mode and a lock flag before its file labels"
        )
    if sequence_mode and sequence_mode not in RUN_SEQUENCE_MODES:
        complaints.append(
            "run sequence mode %r is not one of 'T', 'S' or 'D'" % printable(sequence_mode)
        )
    if lock and lock not in (c.FILE_LOCKED, c.FILE_UNLOCKED):
        complaints.append("run sequence lock flag %r is neither 'L' nor 'U'" % printable(lock))

    if sequence_mode:
        spans.append(
            Span(
                SpanKind.COMMAND,
                offset + 2,
                sequence_mode,
                "order %s" % printable(sequence_mode),
                "Play the files %s"
                % RUN_SEQUENCE_MODES.get(sequence_mode, "in a way this tool does not recognise"),
            )
        )
    if lock:
        spans.append(
            Span(
                SpanKind.COMMAND,
                offset + 3,
                lock,
                "lock %s" % printable(lock),
                "Whether the infrared keyboard may change the sequence",
            )
        )
    if labels:
        spans.append(
            Span(
                SpanKind.COMMAND,
                offset + 4,
                b"".join(labels),
                "sequence %s" % " ".join(printable(one) for one in labels),
                "The TEXT files the sign cycles, in this order, with no host "
                "involvement once it is set",
            )
        )

    shown = " ".join(printable(one) for one in labels)
    return SetRunSequence(
        code=code,
        name="Set run sequence",
        summary="Set run sequence: %s" % (shown or "empty, so the sign plays nothing"),
        details=(
            Detail("Order", RUN_SEQUENCE_MODES.get(sequence_mode, "unknown"),
                   printable(sequence_mode)),
            Detail("Locked", "yes" if lock == c.FILE_LOCKED else "no"),
            Detail("Files", shown or "none"),
        ),
        spans=tuple(spans),
        complaints=tuple(complaints),
        sequence_mode=sequence_mode,
        locked=lock == c.FILE_LOCKED,
        labels=labels,
    )


def _set_time(code: bytes, parameter: bytes, offset: int, spans: list[Span]) -> Command:
    """Decode the set time special function, whose parameter is HHMM."""
    complaints: list[str] = []
    hour = minute = 0
    text = printable(parameter)

    if len(parameter) != 4 or not parameter.isdigit():
        complaints.append("the time should be four digits as HHMM, and this is %r" % text)
    else:
        hour = int(parameter[0:2])
        minute = int(parameter[2:4])
        if hour > 23 or minute > 59:
            complaints.append("%02d:%02d is not a time of day" % (hour, minute))

    spans.append(
        Span(SpanKind.COMMAND, offset + 2, parameter, "time %s" % text,
             "The sign's clock, as HHMM on a 24 hour clock")
    )
    return SetTime(
        code=code,
        name="Set time of day",
        summary="Set the sign's clock to %s" % text,
        details=(Detail("Time", "%02d:%02d" % (hour, minute), text),),
        spans=tuple(spans),
        complaints=tuple(complaints),
        hour=hour,
        minute=minute,
    )


def _set_day(code: bytes, parameter: bytes, offset: int, spans: list[Span]) -> Command:
    """Decode the set day of week special function, 1 for Sunday to 7 for Saturday."""
    complaints: list[str] = []
    text = printable(parameter)
    day = 0

    if len(parameter) != 1 or not parameter.isdigit() or not 1 <= int(parameter) <= 7:
        complaints.append(
            "the day of week should be a single digit from 1 for Sunday to 7 for "
            "Saturday, and this is %r" % text
        )
    else:
        day = int(parameter)

    spans.append(
        Span(SpanKind.COMMAND, offset + 2, parameter, "day %s" % text,
             "The sign's day of week, 1 for Sunday through 7 for Saturday")
    )
    name = DAY_NAMES[day - 1] if day else "an invalid day"
    return SetDayOfWeek(
        code=code,
        name="Set day of week",
        summary="Set the sign's day of week to %s" % name,
        details=(Detail("Day", name, text),),
        spans=tuple(spans),
        complaints=tuple(complaints),
        day=day,
    )


def _set_time_format(code: bytes, parameter: bytes, offset: int, spans: list[Span]) -> Command:
    """Decode the set time format special function: 'S' standard or 'M' military."""
    complaints: list[str] = []
    text = printable(parameter)
    if parameter not in (b"S", b"M"):
        complaints.append(
            "the time format should be 'S' for a 12 hour clock or 'M' for 24 hour, "
            "and this is %r" % text
        )

    spans.append(
        Span(SpanKind.COMMAND, offset + 2, parameter, "format %s" % text,
             "Whether the sign renders the time on a 12 or 24 hour clock")
    )
    military = parameter == b"M"
    return SetTimeFormat(
        code=code,
        name="Set time format",
        summary="Set the sign's clock to a %s hour format" % ("24" if military else "12"),
        details=(Detail("Format", "24 hour" if military else "12 hour", text),),
        spans=tuple(spans),
        complaints=tuple(complaints),
        military=military,
    )


def _read(payload: bytes, offset: int) -> Command:
    """Decode a read command. The sign would answer; this tool only listens."""
    code = payload[0:1]
    label = payload[1:2]
    spans = [_command_span(code, offset)]
    if label:
        spans.append(
            Span(
                SpanKind.COMMAND,
                offset + 1,
                label,
                "reads %s" % printable(label),
                SPECIAL_FUNCTION_NAMES.get(label, "A label this tool has no reading for"),
            )
        )

    what = SPECIAL_FUNCTION_NAMES.get(label, "file %s" % printable(label))
    return ReadCommand(
        code=code,
        name=COMMAND_NAMES.get(code, "Read"),
        summary="Read the %s. This simulator listens only and sends nothing back" % what,
        details=(
            Detail("Reads", what, printable(label)),
            Detail("Answered", "no", "the simulator is one way; a real sign would reply here"),
        ),
        spans=tuple(spans),
        complaints=(),
        label=label,
    )


def _generic(payload: bytes, offset: int) -> Command:
    """Decode a command the protocol lists but this tool does not model."""
    code = payload[0:1]
    rest = payload[1:]
    spans = [_command_span(code, offset), *annotate(rest, offset=offset + 1)]
    name = COMMAND_NAMES[code]
    return Command(
        code=code,
        name=name,
        summary="%s, %d bytes: %s" % (name, len(rest), _preview(rest)),
        details=(Detail("Payload", _preview(rest)),),
        spans=tuple(spans),
        complaints=(),
    )


def _preview(data: bytes, limit: int = 60) -> str:
    """Render bytes short and safe enough for a one line summary."""
    shown = "".join(
        chr(byte) if 0x20 <= byte <= 0x7E else "." for byte in data[:limit]
    )
    return '"%s"%s' % (shown, "..." if len(data) > limit else "")
