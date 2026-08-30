"""What the sign would be holding, and what it would have done without saying so.

The panels in the window are a view of this class. It keeps the file table, the
contents of each file, the run sequence, the priority file and the clock, and it
applies each decoded command the way the protocol document says the sign does.

The notes are the point of it. A real sign accepts a write to an unconfigured
file, or a message longer than the file it goes in, and shows you the
consequence rather than the cause: a blank panel, or a sentence with its end
missing. Every rule below is quoted in ``docs/protocol-notes.md``, and each one
that fires here produces a line saying what the sign just did quietly.

Nothing here imports Qt, so the rules are testable without a display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from readerboard.protocol import constants as c
from signsim import decode
from signsim.decode import (
    ClearMemory,
    Command,
    MemoryEntry,
    SetDayOfWeek,
    SetMemoryConfig,
    SetRunSequence,
    SetTime,
    SetTimeFormat,
    WriteText,
)
from signsim.spans import annotate, readable


class NoteLevel(Enum):
    """How much a note matters."""

    INFO = "info"
    """Something happened that is worth seeing but is entirely normal."""

    WARNING = "warning"
    """The sign accepted this and the result will not be what was intended."""

    VIOLATION = "violation"
    """The protocol document says this is not allowed, and the sign will not obey it."""


@dataclass(frozen=True, slots=True)
class Note:
    """One thing the sign did or refused, in the words of the protocol."""

    level: NoteLevel
    text: str


@dataclass
class StoredFile:
    """The contents of one TEXT file, as the sign would be holding it."""

    label: bytes
    body: bytes
    mode: bytes
    position: bytes
    truncated_to: int | None = None

    @property
    def visible(self) -> bytes:
        """What the sign kept, which is not always what it was sent."""
        if self.truncated_to is None:
            return self.body
        return self.body[: self.truncated_to]

    @property
    def rendered(self) -> str:
        """The kept message read back as markup, which is how it was written.

        This renders :attr:`visible` rather than :attr:`body`, so a file shown
        here is a file as the sign holds it. A message that overflowed its file
        reads back short, which is the whole point of showing it.
        """
        return readable(annotate(self.visible))


@dataclass
class SignState:
    """Everything the simulated sign holds.

    ``memory_config`` is None until a Set Memory Configuration arrives, which
    matters: before that the sign will accept a write only to the priority file
    or to the default file ``A``.
    """

    memory_config: dict[bytes, MemoryEntry] | None = None
    memory_order: list[bytes] = field(default_factory=list)
    files: dict[bytes, StoredFile] = field(default_factory=dict)
    run_sequence: list[bytes] = field(default_factory=list)
    run_sequence_mode: bytes = b""
    run_sequence_locked: bool = False
    priority: bytes = b""
    hour: int | None = None
    minute: int | None = None
    day: int | None = None
    military_time: bool | None = None
    transmissions: int = 0

    # == state a reader wants =============================================

    @property
    def priority_active(self) -> bool:
        """Whether a priority message is suppressing every other file."""
        return bool(self.priority)

    @property
    def playing(self) -> list[bytes]:
        """The files the sign would actually be cycling right now.

        A run sequence label that names no configured file is skipped, which the
        document is explicit about: "If a File Label is invalid or does not
        exist, the next File Label will be processed."
        """
        if self.priority_active:
            return [c.FILE_PRIORITY]
        return [label for label in self.run_sequence if self._exists(label)]

    @property
    def memory_claimed(self) -> int:
        """Bytes of the sign's pool the current configuration takes."""
        if self.memory_config is None:
            return 0
        return sum(
            entry.capacity + c.FILE_OVERHEAD_BYTES for entry in self.memory_config.values()
        )

    def capacity_of(self, label: bytes) -> int | None:
        """Return the configured size of a file, or None if it has none."""
        if label == c.FILE_PRIORITY:
            return c.PRIORITY_FILE_CAPACITY
        if self.memory_config is None:
            return None
        entry = self.memory_config.get(label)
        return entry.capacity if entry is not None else None

    def reset(self) -> None:
        """Forget everything, as if the sign had just been switched on."""
        self.memory_config = None
        self.memory_order = []
        self.files = {}
        self.run_sequence = []
        self.run_sequence_mode = b""
        self.run_sequence_locked = False
        self.priority = b""
        self.hour = self.minute = self.day = None
        self.military_time = None
        self.transmissions = 0

    # == applying a command ================================================

    def apply(self, command: Command) -> list[Note]:
        """Do to this state what the sign would do, and say what that was."""
        self.transmissions += 1

        if command.malformed:
            # The decoder fills a malformed command's fields with placeholders
            # so the detail pane has something to show. Applying those would
            # overwrite good state with invented values: a truncated clock write
            # would set the clock to 00:00, and a write cut off inside its start
            # of mode would look like the empty priority write that releases an
            # alert. Neither is something a sign would do.
            return [
                Note(
                    NoteLevel.VIOLATION,
                    "This transmission is too incomplete to act on, so the sign is "
                    "left exactly as it was. See the problems listed against it.",
                )
            ]

        if isinstance(command, WriteText):
            return self._write_text(command)
        if isinstance(command, SetMemoryConfig):
            return self._set_memory_config(command)
        if isinstance(command, ClearMemory):
            return self._clear_memory()
        if isinstance(command, SetRunSequence):
            return self._set_run_sequence(command)
        if isinstance(command, SetTime):
            self.hour, self.minute = command.hour, command.minute
            return [Note(NoteLevel.INFO, "The sign's clock is now %02d:%02d"
                         % (command.hour, command.minute))]
        if isinstance(command, SetDayOfWeek):
            self.day = command.day
            name = decode.DAY_NAMES[command.day - 1] if command.day else "an invalid day"
            return [Note(NoteLevel.INFO, "The sign's day of week is now %s" % name)]
        if isinstance(command, SetTimeFormat):
            self.military_time = command.military
            return [
                Note(
                    NoteLevel.INFO,
                    "The sign will render the time on a %s hour clock"
                    % ("24" if command.military else "12"),
                )
            ]

        return []

    # == the rules =========================================================

    def _write_text(self, command: WriteText) -> list[Note]:
        """Store a message, or say why the sign would not have stored it."""
        label = command.label
        notes: list[Note] = []

        if label == c.FILE_PRIORITY:
            return self._write_priority(command)

        if not self._writable(label):
            if self.memory_config is None:
                notes.append(
                    Note(
                        NoteLevel.VIOLATION,
                        "File %s was written before any memory configuration. The "
                        "document allows only the priority file '0' and the default "
                        "file 'A' to be written first, so the sign discards this."
                        % _show(label),
                    )
                )
            else:
                wrong_type = self._wrong_type(label)
                if wrong_type is not None:
                    notes.append(
                        Note(
                            NoteLevel.VIOLATION,
                            "File %s is allocated as a %s file, and a Write TEXT "
                            "cannot address it. The sign discards this."
                            % (_show(label), wrong_type),
                        )
                    )
                else:
                    notes.append(
                        Note(
                            NoteLevel.VIOLATION,
                            "File %s is not in the sign's memory configuration, so "
                            "there is nowhere for this message to go and the sign "
                            "discards it." % _show(label),
                        )
                    )
            return notes

        body = command.body
        capacity = self.capacity_of(label)
        truncated_to: int | None = None

        # The sign hands whatever is left of the memory pool to the first file
        # in the configuration once it starts running, so that file is larger
        # than its configured size and a length check against it means nothing.
        first_configured = self.memory_order[0] if self.memory_order else None

        if capacity is not None and len(body) > capacity and label != first_configured:
            truncated_to = capacity
            notes.append(
                Note(
                    NoteLevel.WARNING,
                    "This message is %d bytes and file %s holds %d, so the sign keeps "
                    "the first %d and the rest never appears."
                    % (len(body), _show(label), capacity, capacity),
                )
            )
        elif capacity is not None and len(body) > capacity:
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "This message is %d bytes and file %s was configured for %d, but "
                    "it is the first file in the configuration, and the sign gives "
                    "that one whatever is left of the memory pool. It probably fits."
                    % (len(body), _show(label), capacity),
                )
            )

        # A Write TEXT with no start of mode carries no mode or position, and
        # the document says the sign keeps the ones the file already had. Taking
        # the decoder's empty values here would blank them, which is the
        # opposite of what the detail pane says one pane over.
        previous = self.files.get(label)
        mode = command.mode
        position = command.position
        if not command.has_start_of_mode and previous is not None:
            mode = previous.mode
            position = previous.position

        self.files[label] = StoredFile(
            label=label,
            body=body,
            mode=mode,
            position=position,
            truncated_to=truncated_to,
        )

        if self.priority_active:
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "File %s was updated while a priority message is up. The write "
                    "lands, but nothing of it shows until the priority file is "
                    "released." % _show(label),
                )
            )
        elif body and label not in self.run_sequence:
            # Only worth mentioning, not worth warning about. The service writes
            # the file and then rewrites the run sequence to name it, so this is
            # true of almost every ordinary write for exactly one transmission.
            # A file that stays here is the interesting case, and the run
            # sequence tab is where that shows.
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "File %s holds a message that the run sequence does not name yet, "
                    "so none of it shows. A write is normally followed by a sequence "
                    "naming it." % _show(label),
                )
            )

        return notes

    def _write_priority(self, command: WriteText) -> list[Note]:
        """Take over the sign, or hand it back."""
        body = command.body
        was_active = self.priority_active

        if not body:
            self.priority = b""
            if not was_active:
                return [
                    Note(
                        NoteLevel.INFO,
                        "An empty priority write with no priority message running. "
                        "Harmless, and it is how an alert is released.",
                    )
                ]
            return [
                Note(
                    NoteLevel.INFO,
                    "The priority message is released. The sign resumes its run "
                    "sequence: %s" % (_show_labels(self.playing) or "nothing, it is empty"),
                )
            ]

        kept = body[: c.PRIORITY_FILE_CAPACITY]
        self.priority = kept
        notes = [
            Note(
                NoteLevel.INFO,
                "A priority message is up. Every other TEXT file stops being "
                "displayed until an empty priority write releases it.",
            )
        ]
        if len(body) > c.PRIORITY_FILE_CAPACITY:
            notes.append(
                Note(
                    NoteLevel.WARNING,
                    "The priority file is a fixed %d bytes and this message is %d, so "
                    "the end of it is lost." % (c.PRIORITY_FILE_CAPACITY, len(body)),
                )
            )
        return notes

    def _set_memory_config(self, command: SetMemoryConfig) -> list[Note]:
        """Allocate the file table, erasing every file in the memory pool.

        Not the priority file. It always exists, it sits outside the pool, and
        the document lists exactly four things that cancel a running priority
        message: an empty priority write, a write to the run time table, a write
        to the run day table, and the PROG key. A memory configuration is not
        one of them, so an alert that is up stays up through a reconfiguration.
        """
        erased = sorted(self.files)
        notes: list[Note] = []

        if erased:
            notes.append(
                Note(
                    NoteLevel.WARNING,
                    "Writing a memory configuration overwrote the previous table, so "
                    "the %d message(s) in the memory pool are gone: %s"
                    % (len(erased), _show_labels(erased)),
                )
            )
        else:
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "Memory configured. Any message already in the pool would have "
                    "been erased by this, and there were none.",
                )
            )

        seen: set[bytes] = set()
        table: dict[bytes, MemoryEntry] = {}
        order: list[bytes] = []
        for entry in command.entries:
            if entry.label in seen:
                notes.append(
                    Note(
                        NoteLevel.VIOLATION,
                        "File %s appears twice in this memory configuration."
                        % _show(entry.label),
                    )
                )
                continue
            seen.add(entry.label)
            table[entry.label] = entry
            order.append(entry.label)

        self.memory_config = table
        self.memory_order = order
        self.files = {}

        if order:
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "File %s is first in the configuration, so the sign gives it "
                    "whatever is left of the memory pool. Its real size will not "
                    "match the %d bytes asked for here."
                    % (_show(order[0]), table[order[0]].capacity),
                )
            )

        dangling = [label for label in self.run_sequence if not self._exists(label)]
        if dangling:
            notes.append(
                Note(
                    NoteLevel.WARNING,
                    "The run sequence still names %s, which this configuration does "
                    "not allocate. The sign skips a label that does not exist."
                    % _show_labels(dangling),
                )
            )

        return notes

    def _clear_memory(self) -> list[Note]:
        """Wipe the file table and the pool, leaving the priority file alone."""
        had = len(self.files)
        self.memory_config = None
        self.memory_order = []
        self.files = {}
        return [
            Note(
                NoteLevel.WARNING,
                "Memory cleared. The sign now holds no file table and no pool "
                "messages%s. Nothing but the priority file and the default file 'A' "
                "can be written until a memory configuration arrives. A priority "
                "message that is up stays up: the priority file is outside the pool "
                "and a memory write is not one of the four things that cancel it."
                % ("" if not had else ", and %d message(s) were lost" % had),
            )
        ]

    def _set_run_sequence(self, command: SetRunSequence) -> list[Note]:
        """Choose which files play, and in what order."""
        notes: list[Note] = []

        if self.priority_active:
            notes.append(
                Note(
                    NoteLevel.WARNING,
                    "A run sequence was written while a priority message is up. The "
                    "document lists what cancels a priority message and does not say "
                    "either way about this one, which is why the service holds these "
                    "writes back until the alert is released.",
                )
            )

        seen: set[bytes] = set()
        for label in command.labels:
            if label in seen:
                notes.append(
                    Note(
                        NoteLevel.VIOLATION,
                        "File %s is named twice in this run sequence." % _show(label),
                    )
                )
            seen.add(label)

        self.run_sequence = list(command.labels)
        self.run_sequence_mode = command.sequence_mode
        self.run_sequence_locked = command.locked

        missing = [label for label in command.labels if not self._exists(label)]
        if missing:
            notes.append(
                Note(
                    NoteLevel.WARNING,
                    "The sequence names %s, which the memory configuration does not "
                    "allocate. The sign processes the next label instead of failing."
                    % _show_labels(missing),
                )
            )

        empty = [
            label
            for label in command.labels
            if self._exists(label) and not self.files.get(label, StoredFile(label, b"", b"", b"")).body
        ]
        if empty:
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "The sequence names %s, which exist but hold no message yet."
                    % _show_labels(empty),
                )
            )

        if not command.labels:
            notes.append(
                Note(
                    NoteLevel.INFO,
                    "An empty run sequence. The sign plays nothing from the pool, "
                    "which is what an emptied registry looks like.",
                )
            )

        return notes

    # == helpers ===========================================================

    def _exists(self, label: bytes) -> bool:
        """Whether the sign has a TEXT file by this label to play.

        A run sequence names TEXT files. A label allocated as a STRING or a
        DOTS picture is not one, so it is skipped the same way a label naming
        nothing at all is.
        """
        if self.memory_config is None:
            return label == b"A"
        return self._is_text_file(label)

    def _writable(self, label: bytes) -> bool:
        """Whether a Write TEXT to this label would land.

        "A message file cannot be written until a Memory Configuration is
        written first, unless the file is a Priority TEXT file or the default
        TEXT file A."

        The file also has to be a TEXT file. A Write TEXT cannot address a
        label the configuration allocated as a STRING or a DOTS picture.
        """
        if label == c.FILE_PRIORITY:
            return True
        if self.memory_config is None:
            return label == b"A"
        return self._is_text_file(label)

    def _is_text_file(self, label: bytes) -> bool:
        """Whether the configuration allocated this label as a TEXT file."""
        if self.memory_config is None:
            return False
        entry = self.memory_config.get(label)
        return entry is not None and entry.file_type == c.FILE_TYPE_TEXT

    def _wrong_type(self, label: bytes) -> str | None:
        """Name the type this label was allocated as, when it is not TEXT."""
        if self.memory_config is None:
            return None
        entry = self.memory_config.get(label)
        if entry is None or entry.file_type == c.FILE_TYPE_TEXT:
            return None
        return decode.FILE_TYPE_NAMES.get(entry.file_type, "an unlisted type")


def _show(label: bytes) -> str:
    """Render a file label the way a person would say it."""
    return label.decode("latin-1")


def _show_labels(labels: list[bytes]) -> str:
    """Render a list of file labels, comma separated."""
    return ", ".join(_show(label) for label in labels)
