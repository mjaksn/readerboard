"""The dots each icon is drawn with, as the service described them.

``GET /enumerations/icons/bitmaps`` answers every icon's rows and names the
colour of every character in them, so nothing about an icon's shape is known
here before that call is made. That is the rule the enumerations follow too,
and the reason this is a set of its own rather than a copy of the library.

The one thing this module does know is what each colour word looks like on a
screen, because the service has no way to say and a thumbnail has to be painted
in something. Those are the same shades the sign simulator paints its picture
files in, which the lint job in CI checks, since the simulator's table is in a
module that imports Qt. A word not among them is drawn in magenta rather than
skipped, as the simulator does with a pixel code it does not have, because
something conspicuous beats a hole that reads as an unlit dot.

The parsing is strict for the same reason the enumerations' is: a bitmap that
is quietly wrong looks exactly like a right one at seven dots high.

Two pieces of shape are taken from the service's description of this endpoint
rather than from an answer, and both are written down there: ``#`` is the ink a
tint replaces, and a tint is asked for by adding ``:colour`` inside the tag, as
``<icon:check:red>``. Everything else, including which colour words a tint may
be, is loaded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# The character every bitmap uses for an unlit dot. It is the one character the
# service does not name in ``inks``, so it is the one this has to know.
UNLIT = "."

# The character standing for the ink a tint replaces. An icon whose inks name
# it takes a tint; one whose inks do not is drawn in fixed colours.
TINT_MARKER = "#"

# The most rows and columns a bitmap may have, which are the most a SMALL DOTS
# PICTURE can have by the protocol's Table 22. No sign holds a bigger one, and a
# bitmap past them is not an icon but an answer that would have this client
# allocate and paint a picture the size of the screen several times over.
MAX_ROWS = 31
MAX_COLUMNS = 255

# What each colour word is painted as. Taken from the sign simulator's picture
# panel, so a thumbnail here and a picture file there agree, and checked against
# it by the lint job in CI.
COLOURS = {
    "red": "#ff3a24",
    "green": "#36e05c",
    "amber": "#ffae00",
    "dimred": "#99261c",
    "dimgreen": "#218f40",
    "brown": "#9c5b1f",
    "orange": "#ff761a",
    "yellow": "#ffe660",
}

# The ground behind the dots, the colour of an unlit one, and the colour of a
# word this client has no shade for.
GROUND = "#0c0d0e"
UNLIT_COLOUR = "#2b1f18"
STRAY = "#ff00ff"


class MalformedBitmap(Exception):
    """Raised when a payload does not look like the bitmaps it should be."""


@dataclass(frozen=True, slots=True)
class Bitmap:
    """One icon's dots, and the colour word each character in them stands for."""

    name: str
    rows: tuple[str, ...]
    inks: dict[str, str]

    @property
    def width(self) -> int:
        """How many dots wide it is."""
        return len(self.rows[0]) if self.rows else 0

    @property
    def height(self) -> int:
        """How many dots high it is."""
        return len(self.rows)

    @property
    def takes_tint(self) -> bool:
        """Whether a colour may be asked for, which is whether it has a tint ink."""
        return TINT_MARKER in self.inks

    def tinted(self, word: str | None) -> Bitmap:
        """Return the bitmap as it is drawn with a tint, or itself if it takes none."""
        if not word or not self.takes_tint:
            return self
        return Bitmap(name=self.name, rows=self.rows, inks={**self.inks, TINT_MARKER: word})

    def colour_at(self, x: int, y: int) -> str:
        """Return the screen colour to paint one dot in."""
        dot = self.rows[y][x]
        if dot == UNLIT:
            return UNLIT_COLOUR
        return colour_for(self.inks.get(dot, ""))


@dataclass(frozen=True, slots=True)
class LoadedBitmaps:
    """The bitmaps that have been fetched, and the provenance to show beside them."""

    by_name: dict[str, Bitmap]
    endpoint: str
    loaded_at: datetime

    def summary(self) -> str:
        """Return the one line the enumerations panel shows for them."""
        count = len(self.by_name)
        return "%d bitmap%s, loaded at %s" % (
            count,
            "" if count == 1 else "s",
            self.loaded_at.strftime("%H:%M:%S"),
        )

    def provenance(self) -> str:
        """Return where they came from, in full, for the tooltip."""
        return "%d from %s at %s" % (
            len(self.by_name),
            self.endpoint,
            self.loaded_at.strftime("%H:%M:%S"),
        )


def tinted_tag(name: str, word: str | None) -> str:
    """Return the tag that draws an icon in a tint, as <icon:check:red>.

    The colour goes inside the closing bracket. A name not shaped like a tag is
    handed back as it is, since there is nowhere in it to put a colour.
    """
    if not word or not name.endswith(">"):
        return name
    return "%s:%s>" % (name[:-1], word)


def colour_for(word: str) -> str:
    """Return the screen colour for a colour word, or magenta for one this does not know."""
    return COLOURS.get(word, STRAY)


def parse(payload: object) -> dict[str, Bitmap]:
    """Turn a bitmaps response into bitmaps by name, refusing anything else."""
    if not isinstance(payload, list):
        raise MalformedBitmap("expected a list of bitmaps, got %s" % type(payload).__name__)

    found: dict[str, Bitmap] = {}
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise MalformedBitmap(
                "entry %d is %s, not an object" % (index, type(item).__name__)
            )
        missing = [key for key in ("name", "rows", "inks") if key not in item]
        if missing:
            raise MalformedBitmap(
                "entry %d has no %s; the fields present are %s"
                % (
                    index,
                    " or ".join(repr(key) for key in missing),
                    ", ".join(sorted(item)) or "none",
                )
            )
        name = str(item["name"])
        found[name] = Bitmap(
            name=name,
            rows=_rows(name, item["rows"]),
            inks=_inks(name, item["inks"]),
        )
        _check_every_dot_is_named(found[name])
    return found


def _rows(name: str, rows: object) -> tuple[str, ...]:
    """Return the rows of one bitmap, refusing a ragged or empty one."""
    if not isinstance(rows, list) or not rows or not all(isinstance(row, str) for row in rows):
        raise MalformedBitmap("%s has no rows of dots" % name)
    widths = {len(row) for row in rows}
    if len(widths) != 1 or 0 in widths:
        raise MalformedBitmap(
            "%s has rows of %s dots, not one width"
            % (name, ", ".join(str(width) for width in sorted(widths)))
        )
    (width,) = widths
    if len(rows) > MAX_ROWS or width > MAX_COLUMNS:
        raise MalformedBitmap(
            "%s is %d rows of %d dots, and no picture file holds more than %d rows of %d"
            % (name, len(rows), width, MAX_ROWS, MAX_COLUMNS)
        )
    return tuple(rows)


def _inks(name: str, inks: object) -> dict[str, str]:
    """Return what each character of one bitmap stands for."""
    if not isinstance(inks, dict):
        raise MalformedBitmap(
            "%s names its inks as %s, not an object" % (name, type(inks).__name__)
        )
    for key, value in inks.items():
        if not isinstance(key, str) or len(key) != 1 or not isinstance(value, str):
            raise MalformedBitmap(
                "%s has an ink %r: %r that is not a character and a word" % (name, key, value)
            )
    return dict(inks)


def _check_every_dot_is_named(bitmap: Bitmap) -> None:
    """Refuse a bitmap with a lit dot whose colour nobody said."""
    used = {dot for row in bitmap.rows for dot in row} - {UNLIT}
    unnamed = used - set(bitmap.inks)
    if unnamed:
        raise MalformedBitmap(
            "%s draws %s and names no colour for %s"
            % (
                bitmap.name,
                ", ".join(repr(dot) for dot in sorted(unnamed)),
                "it" if len(unnamed) == 1 else "them",
            )
        )
