"""Deciding which sign file each message lives in.

The sign accepts any printable file label, so the real ceiling on how many
messages can share it is the memory pool in bytes rather than a count of files.
This service hands out ``A`` through ``Z`` only, 26 in all, beside the priority
file ``0`` the sign allocates itself. It allocates a fixed pool of those TEXT
files once and then hands them out to registered messages, one file each.

The pool is fixed rather than grown on demand for one reason: allocating files
erases the sign. Growing the pool when a fourth source turned up would blank the
other three, so the size is a configuration decision made up front and the
service simply refuses a message once the pool is full.
"""

from __future__ import annotations

from readerboard.protocol import constants as c
from readerboard.protocol.frames import FileAllocation
from readerboard.sign.state import AppliedLayout


class LayoutFull(RuntimeError):
    """Every file in the pool is already spoken for."""


class Layout:
    """The pool of sign files, and who currently holds each one."""

    def __init__(self, slot_count: int, slot_capacity: int) -> None:
        """Describe a pool of ``slot_count`` files of ``slot_capacity`` bytes each."""
        if not 1 <= slot_count <= len(c.TEXT_FILE_LABELS):
            raise ValueError(
                "slot_count must be between 1 and %d, got %d"
                % (len(c.TEXT_FILE_LABELS), slot_count)
            )
        self.slot_count = slot_count
        self.slot_capacity = slot_capacity
        self.labels: tuple[bytes, ...] = c.TEXT_FILE_LABELS[:slot_count]
        self._assigned: dict[str, bytes] = {}

    # == the memory configuration ==========================================

    def allocations(self) -> list[FileAllocation]:
        """Return the memory configuration entries for the whole pool."""
        return [FileAllocation(label, self.slot_capacity) for label in self.labels]

    def as_applied(self) -> AppliedLayout:
        """Return this layout in the form the state file records."""
        return AppliedLayout(
            slot_count=self.slot_count,
            slot_capacity=self.slot_capacity,
            labels=[label.decode("ascii") for label in self.labels],
        )

    def needs_reconfiguration(self, applied: AppliedLayout | None) -> bool:
        """Whether the sign has to be reallocated, which will erase it."""
        if applied is None:
            return True
        return not applied.matches(self.slot_count, self.slot_capacity)

    # == handing files out ==================================================

    def assign(self, key: str) -> bytes:
        """Return the file this key uses, claiming a free one if it has none.

        Raises :class:`LayoutFull` when the pool is exhausted, which the API
        turns into a 409 rather than quietly dropping somebody's message.
        """
        existing = self._assigned.get(key)
        if existing is not None:
            return existing

        taken = set(self._assigned.values())
        for label in self.labels:
            if label not in taken:
                self._assigned[key] = label
                return label

        raise LayoutFull(
            "all %d message slots are in use. Remove one, or raise slot_count and "
            "restart, which reallocates the sign and clears it." % self.slot_count
        )

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
        ``MessageRegistry._reattach_labels`` catches that and drops the slot.
        """
        if label not in self.labels:
            raise ValueError(
                "file %r is outside the pool A to %s"
                % (label.decode("ascii"), self.labels[-1].decode("ascii"))
            )
        self._assigned[key] = label

    @property
    def assignments(self) -> dict[str, bytes]:
        """A copy of the current key to file mapping."""
        return dict(self._assigned)

    @property
    def free_count(self) -> int:
        """How many files in the pool are unclaimed."""
        return self.slot_count - len(self._assigned)
