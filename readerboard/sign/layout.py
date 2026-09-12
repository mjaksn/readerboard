"""Deciding which sign file each message and each variable lives in.

The sign accepts any printable file label, so the real ceiling on how many
things can share it is the memory pool in bytes rather than a count of files.
This service allocates two fixed pools beside the priority file ``0`` the sign
allocates itself: TEXT files ``A`` through ``Z`` for messages, and STRING files
``a`` through ``z`` for variables, at most 26 of each. Each registered message
gets one TEXT file and each variable one STRING file.

The pools are fixed rather than grown on demand for one reason: allocating files
erases the sign. Growing a pool when a fourth source turned up would blank the
other three, so the sizes are a configuration decision made up front and the
service simply refuses a message or a variable once its pool is full.
"""

from __future__ import annotations

from readerboard.protocol import constants as c
from readerboard.protocol.frames import FileAllocation
from readerboard.sign.state import AppliedLayout


class LayoutFull(RuntimeError):
    """Every file in the pool is already spoken for."""


class FilePool:
    """A run of sign files, and which key currently holds each one."""

    def __init__(self, labels: tuple[bytes, ...], capacity: int, *, full: str) -> None:
        """Describe a pool of ``labels``, each ``capacity`` bytes, refusing with ``full`` when exhausted."""
        self.labels = labels
        self.capacity = capacity
        self._full = full
        self._assigned: dict[str, bytes] = {}

    def assign(self, key: str) -> bytes:
        """Return the file this key uses, claiming a free one if it has none.

        Raises :class:`LayoutFull` when the pool is exhausted, which the API
        turns into a 409 rather than quietly dropping somebody's write.
        """
        existing = self._assigned.get(key)
        if existing is not None:
            return existing

        taken = set(self._assigned.values())
        for label in self.labels:
            if label not in taken:
                self._assigned[key] = label
                return label

        raise LayoutFull(self._full)

    def release(self, key: str) -> bytes | None:
        """Give a key's file back to the pool, returning the file it held."""
        return self._assigned.pop(key, None)

    def label_for(self, key: str) -> bytes | None:
        """Return the file this key holds, or None if it holds none."""
        return self._assigned.get(key)

    def restore(self, key: str, label: bytes) -> None:
        """Re-establish an assignment read back from the state file.

        A pool that shrank between runs can leave a key pointing at a file that
        no longer exists. Such a key is refused here with a :class:`ValueError`
        rather than silently moved to a free file, since moving it would mean
        writing to a file the sign has not allocated.
        ``SlotRegistry._reattach_labels`` catches that and drops the key.
        """
        if label not in self.labels:
            raise ValueError("file %r is outside the pool as it now stands" % label.decode("ascii"))
        self._assigned[key] = label

    @property
    def assignments(self) -> dict[str, bytes]:
        """A copy of the current key to file mapping."""
        return dict(self._assigned)

    @property
    def free_count(self) -> int:
        """How many files in the pool are unclaimed."""
        return len(self.labels) - len(self._assigned)


class Layout:
    """The sign's two pools of files: TEXT files for messages, STRING files for variables."""

    def __init__(
        self,
        slot_count: int,
        slot_capacity: int,
        variable_count: int = 0,
        variable_capacity: int = 32,
    ) -> None:
        """Describe ``slot_count`` TEXT files and ``variable_count`` STRING files and their sizes."""
        if not 1 <= slot_count <= len(c.TEXT_FILE_LABELS):
            raise ValueError(
                "slot_count must be between 1 and %d, got %d"
                % (len(c.TEXT_FILE_LABELS), slot_count)
            )
        if not 0 <= variable_count <= len(c.STRING_FILE_LABELS):
            raise ValueError(
                "variable_count must be between 0 and %d, got %d"
                % (len(c.STRING_FILE_LABELS), variable_count)
            )
        if not 1 <= variable_capacity <= c.STRING_FILE_CAPACITY:
            raise ValueError(
                "variable_capacity must be between 1 and %d, got %d"
                % (c.STRING_FILE_CAPACITY, variable_capacity)
            )
        self.slot_count = slot_count
        self.slot_capacity = slot_capacity
        self.variable_count = variable_count
        self.variable_capacity = variable_capacity
        self.slots = FilePool(
            c.TEXT_FILE_LABELS[:slot_count],
            slot_capacity,
            full=(
                "all %d message slots are in use. Remove one, or raise slot_count and "
                "restart, which reallocates the sign and clears it." % slot_count
            ),
        )
        self.variables = FilePool(
            c.STRING_FILE_LABELS[:variable_count],
            variable_capacity,
            full=(
                "all %d variables are in use. Delete one, or raise variable_count and "
                "restart, which reallocates the sign and clears it." % variable_count
            ),
        )

    def allocations(self) -> list[FileAllocation]:
        """Return the memory configuration entries for both pools, TEXT files first."""
        return [FileAllocation(label, self.slot_capacity) for label in self.slots.labels] + [
            FileAllocation.string(label, self.variable_capacity) for label in self.variables.labels
        ]

    def as_applied(self) -> AppliedLayout:
        """Return this layout in the form the state file records."""
        return AppliedLayout(
            slot_count=self.slot_count,
            slot_capacity=self.slot_capacity,
            labels=[label.decode("ascii") for label in self.slots.labels],
            variable_count=self.variable_count,
            variable_capacity=self.variable_capacity if self.variable_count else 0,
            variable_labels=[label.decode("ascii") for label in self.variables.labels],
        )

    def needs_reconfiguration(self, applied: AppliedLayout | None) -> bool:
        """Whether the sign has to be reallocated, which will erase it."""
        if applied is None:
            return True
        return not applied.matches(
            self.slot_count, self.slot_capacity, self.variable_count, self.variable_capacity
        )
