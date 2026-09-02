"""Turn a markup string into the bytes a TEXT file holds.

The message language is deliberately small: printable text, plus tokens written
as ``<name>``. ``<red>Hello <degree>`` is a colour change, the word Hello, and a
degree symbol.

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

from readerboard.protocol import constants as c
from readerboard.protocol.tokens import MARKUP_BY_TEXT


class MarkupError(ValueError):
    """A message could not be rendered exactly as written."""


# Characters outside plain ASCII that the sign can render, mapped to the single
# byte that renders them. Taken from the extended character table in the
# protocol document.
EXTENDED_CHARACTERS: dict[str, bytes] = {
    "°": c.DEGREES,
    "¢": c.CENTS,
    "£": c.POUNDS,
    "¥": c.YEN,
    "¿": c.INVERT_QUESTION,
    "¡": c.INVERT_EXCLAIM,
    "Ä": c.A_UMLAUT,
    "Å": c.A_CIRCLE,
    "Æ": c.AE_LIGATURE,
    "Ç": c.C_TAIL,
    "É": c.E_ACCENT,
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
_TAG_CHARACTERS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def render(message: str, *, strict: bool = True) -> bytes:
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
    """
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
