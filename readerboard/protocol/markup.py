"""Turn a markup string into the bytes a TEXT file holds.

The message language is deliberately small: printable text, plus tokens written
as ``<name>``. ``<red>Hello <degree>`` is a colour change, the word Hello, and a
degree symbol.

Two tags are not tokens. ``<var:name>`` calls a variable, which lives in a
STRING file of its own, and ``<icon:name>`` calls an icon, which lives in a
SMALL DOTS PICTURE file. Each renders as the two bytes that call that file.

Both work the same way here, and the way is deliberate: the renderer is told
which file each name lives in and knows nothing else about either. It does not
hold the icon library and cannot say whether an icon exists, so the registry
that hands the files out stays the one place that decides. That is what stops
two answers to "is there an icon called that" from drifting apart.

Two rules here are load bearing, and both have an obvious wrong answer.

First, the cursor advances on every branch. A tokenizer that only moves forward
inside the branch that found a closing ``>`` will spin forever on a message
containing a bare ``<``, wedging whichever thread is rendering it. The loop body
below always moves ``index``, whatever it decided about the character it just
looked at.

Second, text is encoded against the sign's own character table, not as UTF-8.
The sign does not understand UTF-8, so encoding it that way puts two bytes of
noise on the display in place of each accented letter. Characters the sign can
render are mapped to it, and characters it cannot are either rejected or
replaced depending on how strict the caller asked us to be.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from readerboard.protocol import constants as c
from readerboard.protocol.tokens import MARKUP_BY_TEXT, MARKUP_TOKENS


class MarkupError(ValueError):
    """A message could not be rendered exactly as written."""


# The Unicode character a caller writes to ask for the sign's single column
# space. U+2009 THIN SPACE is the closest thing Unicode has to it.
THIN_SPACE = "\u2009"


# Characters outside plain ASCII that the sign can render, mapped to the single
# byte that renders them. Taken from the extended character table in the
# protocol document.
#
# This map is the only way to reach a character the sign has, so a code missing
# from it is a glyph nobody can draw. Forty-two of the sixty-six were mapped
# here and the rest were not, which is why a message containing Á was answered
# with "the sign cannot display 'Á'" by a service talking to a sign that can.
#
# What held the rest back was that the document draws its character column as
# vector outlines, so nothing established which mark a code held. That was
# settled on 2026-09-10 by reading the three table pages as images, and the ten
# added below are the ones whose glyph is unambiguous at that resolution. Each
# was then drawn on the sign beside the character it is mapped from, because a
# mapping asserts an identity and a scan read wrongly would put a silently
# different glyph on the display.
#
# The rest of the table stays unmapped on purpose. Codes B0H to B9H look like a
# Croatian or Serbian set and the diacritics at BBH to BDH cannot be told apart
# at five by seven, and guessing one would be worse than leaving it out: an
# unmapped character is refused with a message saying so, while a wrong one is
# accepted and drawn.
EXTENDED_CHARACTERS: dict[str, bytes] = {
    "°": c.DEGREES,
    "¢": c.CENTS,
    "£": c.POUNDS,
    "¥": c.YEN,
    "₧": c.PESETA,
    "ƒ": c.SLANT_F,
    "¿": c.INVERT_QUESTION,
    "¡": c.INVERT_EXCLAIM,
    "ª": c.SUPER_a,
    "º": c.SUPER_o,
    "θ": c.theta,
    "Θ": c.THETA,
    # A single column space, narrower than the half space at 7EH. Named rather
    # than written as itself, because a space character sitting in a dict key is
    # invisible to whoever reads this next.
    THIN_SPACE: c.SINGLE_COL_SPACE,
    "Á": c.A_ACCENT,
    "Ä": c.A_UMLAUT,
    "Å": c.A_CIRCLE,
    "Æ": c.AE_LIGATURE,
    "Ç": c.C_TAIL,
    "É": c.E_ACCENT,
    "Ê": c.E_ACCENT_HAT,
    "Í": c.I_ACCENT,
    "Ñ": c.N_TILDE,
    "Õ": c.O_TILDE,
    "Ö": c.O_UMLAUT,
    "Ü": c.U_UMLAUT,
    "ß": c.BETA,
    "à": c.a_GRAVE,
    "á": c.a_ACCENT,
    "â": c.a_CIRCUMFLEX,
    "ä": c.a_UMLAUT,
    "å": c.a_CIRCLE,
    "æ": c.ae_LIGATURE,
    "ç": c.c_TAIL,
    "è": c.e_GRAVE,
    "é": c.e_ACCENT,
    "ê": c.e_CIRCUMFLEX,
    "ë": c.e_UMLAUT,
    "ì": c.i_GRAVE,
    "í": c.i_ACCENT,
    "î": c.i_CIRCUMFLEX,
    "ï": c.i_UMLAUT,
    "ñ": c.n_TILDE,
    "ò": c.o_GRAVE,
    "ó": c.o_ACCENT,
    "ô": c.o_CIRCUMFLEX,
    "õ": c.o_TILDE,
    "ö": c.o_UMLAUT,
    "ù": c.u_GRAVE,
    "ú": c.u_ACCENT,
    "û": c.u_CIRCUMFLEX,
    "ü": c.u_UMLAUT,
    "ÿ": c.y_UMLAUT,
}

# What an unrenderable character becomes when the caller is not being strict.
REPLACEMENT = b"?"

# The tag name may only contain these, which keeps a stray "<" in prose such as
# "a < b" from being mistaken for the start of a tag that runs to the next ">".
# The colon is there for the one tag that carries an argument, <var:name>.
_TAG_CHARACTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:")

VARIABLE_TAG_PREFIX = "<var:"
ICON_TAG_PREFIX = "<icon:"

# What a variable may be called. Narrower than a slot key on purpose: it has to
# sit inside a tag, and a name the tag cannot hold is a variable no message can
# call. The API validates the name in its path against the same pattern.
VARIABLE_NAME_PATTERN = r"^[a-z0-9_]{1,32}$"
_VARIABLE_NAME = re.compile(VARIABLE_NAME_PATTERN)

# What an icon may be called, and what may follow it. The name takes a
# variable's shape for the same reason, and the optional second part is the
# colour word a tintable icon is drawn in, which is one of the colour tokens'
# own spellings so that a caller who can write <red> need learn nothing new.
# Which words are colours, and which icons take one, are the icon library's to
# know and not this module's. See the note at the top about what the renderer
# is not told.
ICON_NAME_PATTERN = r"^[a-z0-9_]{1,32}$"
ICON_TINT_PATTERN = r"^[a-z]{1,16}$"
_ICON_TAG = re.compile(r"^([a-z0-9_]{1,32})(?::([a-z]{1,16}))?$")

# Every date insert starts with 0BH. Inside a STRING file the sign drops that
# byte and draws the selector after it as a literal character, so <week_day> in
# a value shows a 9 rather than the day. Measured on 2026-09-10; see "STRING
# files, measured on the sign" in docs/protocol-notes.md.
_DATE_INSERT = c.CURDATE_WEEKDAYY[:1]
_NOT_IN_A_VALUE = {
    token.text: (
        "%s cannot go in a variable's value, because the sign draws it there as a "
        "literal character; put it in the message that calls the variable instead"
        % token.text
    )
    for token in MARKUP_TOKENS
    if token.value.startswith(_DATE_INSERT)
}

# The tokens a variable's value may contain: every one a message may, less those.
VALUE_TOKENS = tuple(token for token in MARKUP_TOKENS if token.text not in _NOT_IN_A_VALUE)


def render(
    message: str,
    *,
    strict: bool = True,
    variables: Mapping[str, bytes] | None = None,
    icons: Mapping[tuple[str, str | None], bytes] | None = None,
) -> bytes:
    """Render ``message`` to sign bytes.

    When ``strict`` is true an unknown tag, an unterminated tag or a character
    the sign cannot display raises :class:`MarkupError`, which is what every
    write over HTTP wants: the caller is told rather than shown something it
    did not ask for. When it is false an unknown or unterminated tag is passed
    through as literal text instead, and a character the sign cannot display
    becomes a question mark. Nothing accepts a message that way; the lenient
    path is for re-rendering a message this service already accepted once, so
    that a slot or an alert restored from disk cannot fail to come back because
    the rules around it tightened in the meantime.

    ``variables`` maps each variable name to the STRING file it lives in, and a
    ``<var:name>`` becomes the call to that file. None means no variable can be
    called here at all, which is a different refusal from a map that lacks the
    name. When not strict, a call that cannot be made renders nothing, which is
    also what the sign draws for a call to a STRING that is not there.

    ``icons`` is the same arrangement for pictures, keyed by the icon's name and
    the tint it was asked for, since a tint changes the bitmap and so needs a
    picture file of its own. ``<icon:check>`` and ``<icon:check:red>`` are two
    entries. A call to a picture that is not there draws nothing on the sign too.
    """
    return _render(
        message, strict=strict, variables=variables, icons=icons, in_value=False
    )


def render_value(value: str, *, strict: bool = True) -> bytes:
    """Render a variable's value, the bytes a STRING file holds.

    The message language again, less the things the sign cannot draw from
    inside a STRING: a date insert, which it draws as its selector character, a
    call to another variable, which it draws as the label's letter, and a call
    to a picture, which nobody has put to the sign. That last one is refused on
    suspicion rather than on measurement, and deliberately: the one neighbouring
    code that was tried drew its own label as a letter, and an icon in a value
    is not worth an unexplained character on a wall. The
    document's own list of what a STRING may hold is narrower than this, and
    wrong: rainbow, flash, the attributes and the extended characters it leaves
    out all worked on the sign.

    Formatting in a value is not contained by it. A colour set in a value
    carries on into the message after the call, as a character set and a speed
    do, which the caller has to know and this cannot fix.
    """
    return _render(value, strict=strict, variables=None, icons=None, in_value=True)


def references(message: str) -> list[str]:
    """Return the variable names a message calls, each once, in order of first use.

    Tags are found exactly as :func:`render` finds them, so the two cannot
    disagree about what counts as a call. A name that is not a valid variable
    name is still returned, since the caller wants to know what was asked for.
    """
    names: list[str] = []
    index = 0
    while index < len(message):
        if message[index] == "<":
            tag, after = _read_tag(message, index)
            if tag is not None:
                if tag.startswith(VARIABLE_TAG_PREFIX):
                    name = tag[len(VARIABLE_TAG_PREFIX) : -1]
                    if name not in names:
                        names.append(name)
                index = after
                continue
        index += 1
    return names


def icon_references(message: str) -> list[tuple[str, str | None]]:
    """Return the icons a message calls, each once, in order of first use.

    Each is the icon's name and the tint it was asked for, or None for one drawn
    in its own colours. The pair is what the caller needs, not the name alone: a
    tint changes the bitmap, so ``<icon:check>`` and ``<icon:check:red>`` are two
    different pictures and want a file each.

    Tags are found exactly as :func:`render` finds them, so the two cannot
    disagree about what counts as a call. A tag whose shape :func:`render` would
    refuse is left out, since there is no icon there to name.
    """
    found: list[tuple[str, str | None]] = []
    index = 0
    while index < len(message):
        if message[index] == "<":
            tag, after = _read_tag(message, index)
            if tag is not None:
                if tag.startswith(ICON_TAG_PREFIX):
                    match = _ICON_TAG.match(tag[len(ICON_TAG_PREFIX) : -1])
                    if match is not None:
                        called = (match.group(1), match.group(2))
                        if called not in found:
                            found.append(called)
                index = after
                continue
        index += 1
    return found


def _render(
    message: str,
    *,
    strict: bool,
    variables: Mapping[str, bytes] | None,
    icons: Mapping[tuple[str, str | None], bytes] | None,
    in_value: bool,
) -> bytes:
    out = bytearray()
    index = 0
    length = len(message)

    while index < length:
        char = message[index]

        if char == "<":
            tag, after = _read_tag(message, index)
            if tag is None:
                # Not a tag at all, just a less-than sign in the text.
                if strict:
                    raise MarkupError(
                        "unterminated tag at position %d; write a complete tag such as "
                        "<red>, or remove the '<'" % index
                    )
                out += _encode_character("<", strict=False)
                index += 1
                continue

            if tag.startswith(VARIABLE_TAG_PREFIX):
                out += _call(tag, strict=strict, variables=variables, in_value=in_value)
                index = after
                continue

            if tag.startswith(ICON_TAG_PREFIX):
                out += _draw(tag, strict=strict, icons=icons, in_value=in_value)
                index = after
                continue

            if in_value and tag in _NOT_IN_A_VALUE:
                if strict:
                    raise MarkupError(_NOT_IN_A_VALUE[tag])
                index = after
                continue

            token = MARKUP_BY_TEXT.get(tag)
            if token is None:
                if strict:
                    raise MarkupError("unknown markup token %r" % tag)
                out += _encode_text(tag, strict=False)
            else:
                out += token.value
            index = after
            continue

        out += _encode_character(char, strict=strict)
        index += 1

    return bytes(out)


def _call(
    tag: str,
    *,
    strict: bool,
    variables: Mapping[str, bytes] | None,
    in_value: bool,
) -> bytes:
    """Render one ``<var:name>``, or explain why it cannot be rendered."""
    name = tag[len(VARIABLE_TAG_PREFIX) : -1]
    if in_value:
        problem = (
            "a variable's value cannot call another variable; the sign draws %s there "
            "as a letter" % tag
        )
    elif not _VARIABLE_NAME.match(name):
        problem = (
            "%r is not a variable name; use one to 32 lowercase letters, digits and "
            "underscores" % name
        )
    elif variables is None:
        problem = "%s calls a variable, and variables cannot be used here" % tag
    else:
        label = variables.get(name)
        if label is not None:
            return c.STRING_FILE_INSERT + label
        problem = "there is no variable named %r; create it before a message calls it" % name

    if strict:
        raise MarkupError(problem)
    return b""


def _draw(
    tag: str,
    *,
    strict: bool,
    icons: Mapping[tuple[str, str | None], bytes] | None,
    in_value: bool,
) -> bytes:
    """Render one ``<icon:name>`` or ``<icon:name:tint>``, or explain why it cannot be.

    What is checked here is the shape of the tag and whether a picture holds
    that icon. Whether the icon exists at all, and whether it is one a tint may
    be asked for, belong to the icon library and to the registry that hands out
    the picture files, which refuse those in their own words before a message
    gets this far.
    """
    body = tag[len(ICON_TAG_PREFIX) : -1]
    match = _ICON_TAG.match(body)

    if in_value:
        problem = (
            "a variable's value cannot draw an icon; put %s in the message that calls "
            "the variable instead" % tag
        )
    elif match is None:
        problem = (
            "%r is not an icon; write <icon:name>, or <icon:name:colour> for one that "
            "takes a tint" % body
        )
    elif icons is None:
        problem = "%s draws an icon, and icons cannot be used here" % tag
    else:
        name, tint = match.group(1), match.group(2)
        label = icons.get((name, tint))
        if label is not None:
            return c.DOTS_INSERT + label
        problem = "there is no picture holding %s" % tag

    if strict:
        raise MarkupError(problem)
    return b""


def _read_tag(message: str, start: int) -> tuple[str | None, int]:
    """Read a ``<name>`` beginning at ``start``.

    Returns the tag including its brackets and the index just past it, or
    ``(None, start)`` when what follows is not a well formed tag.
    """
    index = start + 1
    length = len(message)
    while index < length and message[index] in _TAG_CHARACTERS:
        index += 1

    if index < length and message[index] == ">" and index > start + 1:
        return message[start : index + 1], index + 1

    return None, start


def _encode_text(text: str, *, strict: bool) -> bytes:
    out = bytearray()
    for char in text:
        out += _encode_character(char, strict=strict)
    return bytes(out)


def _encode_character(char: str, *, strict: bool) -> bytes:
    code = ord(char)
    if 0x20 <= code <= 0x7E:
        return char.encode("ascii")

    mapped = EXTENDED_CHARACTERS.get(char)
    if mapped is not None:
        return mapped

    if char == "\n":
        return c.CR

    if strict:
        raise MarkupError(
            "the sign cannot display %r (U+%04X); use a markup token or plain ASCII" % (char, code)
        )
    return REPLACEMENT
