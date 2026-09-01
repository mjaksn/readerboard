"""The enumerations the client has been told about, and nothing it assumed.

Nothing here is hardcoded. Every set starts empty and stays empty until someone
presses the button that calls the endpoint, which is the point: a client that
ships its own copy of the markup tokens is a client that goes on offering
``<degree>`` for a year after the service stopped answering it.

All four sets arrive in one shape, ``{"name": ..., "description": ...}``, and
land here as :class:`Entry`. The parsing is still strict about that shape rather
than tolerant of anything list-like, because a set of empty names looks exactly
like a healthy one on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


class MalformedEnumeration(Exception):
    """Raised when a payload does not look like the enumeration it should be."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One name a caller may use, and what the service says it does."""

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class LoadedSet:
    """A set that has been fetched, and the provenance to show beside it."""

    entries: tuple[Entry, ...]
    endpoint: str
    loaded_at: datetime

    @property
    def names(self) -> tuple[str, ...]:
        """Return just the names, in the order the service gave them."""
        return tuple(entry.name for entry in self.entries)

    def summary(self) -> str:
        """Return the one-line provenance the panel shows under the set's title."""
        return "%d from %s at %s" % (
            len(self.entries),
            self.endpoint,
            self.loaded_at.strftime("%H:%M:%S"),
        )


def parse(payload: object) -> tuple[Entry, ...]:
    """Turn an enumeration response into entries, refusing anything else.

    A payload that is a list of objects but not *this* list of objects is
    rejected rather than quietly producing a set of empty names, which is what
    being pointed at a service whose enumerations answer something else looks
    like.
    """
    if not isinstance(payload, list):
        raise MalformedEnumeration(
            "expected a list of entries, got %s" % type(payload).__name__
        )

    entries: list[Entry] = []
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise MalformedEnumeration(
                "entry %d is %s, not an object" % (index, type(item).__name__)
            )
        if "name" not in item:
            raise MalformedEnumeration(
                "entry %d has no 'name'; the fields present are %s"
                % (index, ", ".join(sorted(item)) or "none")
            )
        entries.append(
            Entry(
                name=str(item["name"]),
                description=str(item.get("description", "")),
            )
        )
    return tuple(entries)


@dataclass
class EnumStore:
    """What has been loaded so far, keyed by the client's name for each set."""

    sets: dict[str, LoadedSet] = field(default_factory=dict)

    def load(self, set_key: str, endpoint: str, entries: tuple[Entry, ...]) -> LoadedSet:
        """Record a freshly fetched set, replacing whatever was there before."""
        loaded = LoadedSet(entries=entries, endpoint=endpoint, loaded_at=datetime.now())
        self.sets[set_key] = loaded
        return loaded

    def get(self, set_key: str) -> LoadedSet | None:
        """Return the set if it has been loaded, otherwise None."""
        return self.sets.get(set_key)

    def is_loaded(self, set_key: str) -> bool:
        """Whether this set has been fetched at least once."""
        return set_key in self.sets

    def names(self, set_key: str) -> tuple[str, ...]:
        """Return the names in a set, or an empty tuple if it has not been loaded."""
        loaded = self.sets.get(set_key)
        return loaded.names if loaded else ()

    def describe(self, set_key: str, name: str) -> str:
        """Return the description for one name, or an empty string if unknown."""
        loaded = self.sets.get(set_key)
        if loaded is None:
            return ""
        for entry in loaded.entries:
            if entry.name == name:
                return entry.description
        return ""
