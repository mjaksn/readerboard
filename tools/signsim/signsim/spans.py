r"""Split a rendered message body into runs of text, glyphs and control codes.

This is what turns a wall of hex into something a person can read. A body such
as ``\x1c\x31HI\x08\x49`` is three things: set the colour to red, the letters
HI, and a degree symbol. Colouring those differently, and putting the protocol's
own meaning beside each one, is most of what the log is for.

The tables come from ``readerboard.protocol``, so a token added to the service
is understood here without anything being copied. That is the cheap option and
it has a cost worth stating plainly: a decoder reading the encoder's own table
agrees with it by construction, so it can confirm which token was sent but never
that the token's byte value is the one the protocol document asks for.
``tests/test_constant_values.py`` in the service is what checks that, and it is
the only thing that does.

Scanning is longest match first, because the sequences overlap. ``\x1d\x30\x31``
is three bytes meaning wide characters on, and reading only its first two would
turn the rest of the message into nonsense.

Nothing here imports Qt.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from readerboard.protocol import constants as c
from readerboard.protocol.markup import EXTENDED_CHARACTERS
from readerboard.protocol.tokens import MARKUP_TOKENS


class SpanKind(Enum):
    """What one run of bytes is, which is also what colour it gets."""

    FRAMING = "framing"
    """Wrapper bytes: the wakeup nulls, `SOH`, sign type, address, `STX`, `EOT`."""

    COMMAND = "command"
    """The command code and the file or function label it names."""

    TEXT = "text"
    """Printable ASCII, which the sign renders as itself."""

    GLYPH = "glyph"
    """One byte or pair that renders a single character ASCII has no room for."""

    CONTROL = "control"
    """Changes how what follows is rendered, or inserts a value the sign holds."""

    UNKNOWN = "unknown"
    """Not in any table here. Either a byte the sign will ignore, or a bug."""


@dataclass(frozen=True, slots=True)
class Span:
    """One run of bytes, and what the protocol says it means."""

    kind: SpanKind
    offset: int
    data: bytes
    label: str
    description: str

    @property
    def end(self) -> int:
        """The offset just past this span."""
        return self.offset + len(self.data)


# The three markup tokens that put a character on the sign rather than changing
# how later characters are drawn. Everything else in the table is a control.
_GLYPH_TOKENS = frozenset({"<degree>", "<block>", "<half_space>"})


def _build_table() -> dict[bytes, tuple[SpanKind, str, str]]:
    """Assemble the fixed length sequences, keyed by the bytes that spell them."""
    table: dict[bytes, tuple[SpanKind, str, str]] = {}

    # Everything a caller can write as markup, with the description the service
    # already publishes for it through /v2/tokens.
    for token in MARKUP_TOKENS:
        kind = SpanKind.GLYPH if token.text in _GLYPH_TOKENS else SpanKind.CONTROL
        table[token.value] = (kind, token.text, token.description)

    # Sequences the service never sends but the sign accepts, so that a log of
    # traffic from anything else is still readable.
    extra: tuple[tuple[bytes, str, str], ...] = (
        (c.TRUE_DESCENDERS_ON, "true_descenders_on", "Draw descenders below the baseline"),
        (c.TRUE_DESCENDERS_OFF, "true_descenders_off", "Return to descenders on the baseline"),
        (c.CHARSET_5_NORMAL, "charset_5_normal", "Use the five high normal character set"),
        (c.CHARSET_7_NORMAL, "charset_7_normal", "Use the seven high normal character set"),
        (c.CHARSET_7_FANCY, "charset_7_fancy", "Use the seven high fancy character set"),
        (c.CHARSET_10_NORMAL, "charset_10_normal", "Use the ten high normal character set"),
        (c.CHARSET_FULL_FANCY, "charset_full_fancy", "Use the full height fancy character set"),
        (c.CHARSET_FULL_NORMAL, "charset_full_normal", "Use the full height normal character set"),
        (c.CHAR_ATTRIB_WIDE_ON, "attrib_wide_on", "Character attribute: wide on"),
        (c.CHAR_ATTRIB_WIDE_OFF, "attrib_wide_off", "Character attribute: wide off"),
        (c.CHAR_ATTRIB_DBLW_ON, "attrib_double_width_on", "Character attribute: double width on"),
        (c.CHAR_ATTRIB_DBLW_OFF, "attrib_double_width_off", "Character attribute: double width off"),
        (c.CHAR_ATTRIB_DBLH_ON, "attrib_double_height_on", "Character attribute: double height on"),
        (
            c.CHAR_ATTRIB_DBLH_OFF,
            "attrib_double_height_off",
            "Character attribute: double height off",
        ),
        (c.CHAR_ATTRIB_DESC_ON, "attrib_descenders_on", "Character attribute: true descenders on"),
        (
            c.CHAR_ATTRIB_DESC_OFF,
            "attrib_descenders_off",
            "Character attribute: true descenders off",
        ),
        (c.CHAR_ATTRIB_FIX_ON, "attrib_fixed_width_on", "Character attribute: fixed width on"),
        (c.CHAR_ATTRIB_FIX_OFF, "attrib_fixed_width_off", "Character attribute: fixed width off"),
        (c.CHAR_ATTRIB_FNCY_ON, "attrib_fancy_on", "Character attribute: fancy on"),
        (c.CHAR_ATTRIB_FNCY_OFF, "attrib_fancy_off", "Character attribute: fancy off"),
        (c.TEMP_CELSIUS, "temperature_celsius", "Insert the current temperature in celsius"),
        (
            c.TEMP_FAHRENHEIT,
            "temperature_fahrenheit",
            "Insert the current temperature in fahrenheit",
        ),
        (c.COUNTER_1, "counter_1", "Insert the current value of counter 1"),
        (c.COUNTER_2, "counter_2", "Insert the current value of counter 2"),
        (c.COUNTER_3, "counter_3", "Insert the current value of counter 3"),
        (c.COUNTER_4, "counter_4", "Insert the current value of counter 4"),
        (c.COUNTER_5, "counter_5", "Insert the current value of counter 5"),
        (c.CURDATE_MMDDYY_DASH, "date_mmddyy_dash", "Insert the current date as MM-DD-YY"),
        (c.CURDATE_DDMMYY_DASH, "date_ddmmyy_dash", "Insert the current date as DD-MM-YY"),
        (c.CURDATE_MMDDYY_DOT, "date_mmddyy_dot", "Insert the current date as MM.DD.YY"),
        (c.CURDATE_DDMMYY_DOT, "date_ddmmyy_dot", "Insert the current date as DD.MM.YY"),
        (c.CURDATE_MMDDYY_SPACE, "date_mmddyy_space", "Insert the current date as MM DD YY"),
        (c.CURDATE_DDMMYY_SPACE, "date_ddmmyy_space", "Insert the current date as DD MM YY"),
        (c.LF, "line_feed", "Line feed"),
    )
    for value, label, description in extra:
        table.setdefault(value, (SpanKind.CONTROL, label, description))

    # The single byte extended characters, described by the character they draw
    # rather than by a name, because the character is the useful thing to see.
    for character, value in EXTENDED_CHARACTERS.items():
        table.setdefault(
            value,
            (SpanKind.GLYPH, "char %s" % character, "The sign's own byte for %r" % character),
        )

    return table


_TABLE = _build_table()
_MAX_FIXED_LENGTH = max(len(value) for value in _TABLE)

# High bytes with no entry above are still extended characters; the protocol's
# table runs to 0xC1. Past that the sign has nothing to draw.
_EXTENDED_RANGE = range(0x80, 0xC2)


def annotate(body: bytes, *, offset: int = 0) -> list[Span]:
    """Split ``body`` into spans, numbering them from ``offset``.

    ``offset`` is where ``body`` sits inside the whole payload, so that a span
    can be pointed at directly in a hex view of the transmission.
    """
    spans: list[Span] = []
    text = bytearray()
    text_start = 0

    def flush() -> None:
        if not text:
            return
        spans.append(
            Span(
                kind=SpanKind.TEXT,
                offset=offset + text_start,
                data=bytes(text),
                label="text",
                description="Literal text: %r" % text.decode("ascii"),
            )
        )
        text.clear()

    index = 0
    while index < len(body):
        matched = _match_at(body, index)
        if matched is not None:
            flush()
            length, kind, label, description = matched
            spans.append(
                Span(
                    kind=kind,
                    offset=offset + index,
                    data=body[index : index + length],
                    label=label,
                    description=description,
                )
            )
            index += length
            continue

        byte = body[index]
        if 0x20 <= byte <= 0x7E:
            if not text:
                text_start = index
            text.append(byte)
            index += 1
            continue

        flush()
        spans.append(
            Span(
                kind=SpanKind.UNKNOWN,
                offset=offset + index,
                data=body[index : index + 1],
                label="0x%02X" % byte,
                description="No entry in the protocol's tables for this byte",
            )
        )
        index += 1

    flush()
    return spans


def readable(items: Iterable[Span]) -> str:
    r"""Reassemble spans into something close to the markup that produced them.

    This is what a log row should show. A body of ``\\x1c\\x31HI`` reads back as
    ``<red>HI``, which is both shorter than the hex and the same thing the
    caller wrote in the first place.
    """
    out: list[str] = []
    for span in items:
        if span.kind is SpanKind.TEXT:
            out.append(span.data.decode("ascii"))
        elif span.kind is SpanKind.UNKNOWN:
            out.append("".join("\\x%02x" % byte for byte in span.data))
        elif span.label.startswith("char "):
            out.append(span.label.removeprefix("char "))
        elif span.label.startswith("<"):
            out.append(span.label)
        else:
            out.append("<%s>" % span.label)
    return "".join(out)


def _match_at(body: bytes, index: int) -> tuple[int, SpanKind, str, str] | None:
    """Return the sequence beginning at ``index``, longest first, or None."""
    variable = _match_variable_length(body, index)
    if variable is not None:
        return variable

    for length in range(_MAX_FIXED_LENGTH, 0, -1):
        entry = _TABLE.get(body[index : index + length])
        if entry is not None:
            kind, label, description = entry
            return length, kind, label, description

    byte = body[index]
    if byte in _EXTENDED_RANGE:
        return (
            1,
            SpanKind.GLYPH,
            "extended 0x%02X" % byte,
            "An extended character from the protocol's table, unnamed in this service",
        )

    return None


def _match_variable_length(body: bytes, index: int) -> tuple[int, SpanKind, str, str] | None:
    """Match the sequences whose length depends on what follows them.

    A fixed table cannot hold these: each takes an argument, and the argument is
    arbitrary bytes rather than one of a known set.
    """
    head = body[index : index + 1]
    remaining = len(body) - index

    if head == c.SOM:
        # A mode change inside a body, which is how a multi-page message gives
        # each page its own mode. The service writes one mode per file and never
        # sends this, so seeing it means the traffic came from somewhere else.
        mode_length = 2 if body[index + 2 : index + 3] == b"n" else 1
        length = 2 + mode_length
        if remaining < length:
            return None
        position = body[index + 1 : index + 2].decode("latin-1")
        mode = body[index + 2 : index + 2 + mode_length].decode("latin-1")
        return (
            length,
            SpanKind.CONTROL,
            "mode change",
            "Start of mode: position %r, display mode %r, which begins a new page"
            % (position, mode),
        )

    if head == c.STRING_FILE_INSERT and remaining >= 2:
        name = body[index + 1 : index + 2].decode("latin-1")
        return (
            2,
            SpanKind.CONTROL,
            "insert string %s" % name,
            "Insert the contents of STRING file %r at this point" % name,
        )

    if head == c.DOTS_INSERT and remaining >= 2:
        name = body[index + 1 : index + 2].decode("latin-1")
        return (
            2,
            SpanKind.CONTROL,
            "insert dots %s" % name,
            "Insert DOTS picture %r at this point" % name,
        )

    if head == c.ALPHA_DOTS_INSERT and remaining >= 15:
        argument = body[index + 1 : index + 15].decode("latin-1")
        return (
            15,
            SpanKind.CONTROL,
            "insert alphavision dots",
            "Insert an ALPHAVISION DOTS picture: %r" % argument,
        )

    return None
