"""Read what the sign says back.

Everything here is pure: bytes in, values out. The sign answers a read with a
whole transmission of its own, and the useful part is buried in the middle of
it, so unwrapping is separated from parsing and both are separated from the
input and output that fetched the bytes.

The reply frame, from the worked example in the document:

    <NUL> x20  SOH  '0'  '00'  STX  'E'  label  data  ETX  ...

Three things about that are worth knowing before changing this module.

The type code is ``0``, the Response code, and the address is "sent regardless
of the sign's actual address". So neither identifies which sign answered, and
neither is checked here.

The command code the sign echoes is ``E``, the **write** code, not the ``F``
that was sent. That reads like a mistake and is what the document specifies.

And the leading nulls are the reason a reply cannot be read with one
``read(in_waiting or 1)``. The first byte arrives well before the rest, so an
eager reader returns a lone null byte for every question asked, and two of
those compare equal, which looks like a confirmation and is not.
"""

from __future__ import annotations

from dataclasses import dataclass

from readerboard.protocol import constants as c


class ReplyError(ValueError):
    """The sign answered, but not with something this could read."""


@dataclass(frozen=True, slots=True)
class GeneralInformation:
    """What the sign says about itself.

    ``firmware_released`` is a month and a year and nothing finer, because that
    is all the four digits carry. ``memory_free`` and ``memory_total`` are
    bytes.
    """

    firmware_version: str
    firmware_revision: str
    firmware_released: str
    clock: str
    time_format: str
    speaker_enabled: bool
    memory_total: int
    memory_free: int
    raw: str


def unwrap(reply: bytes, label: bytes) -> bytes:
    """Pull the data field out of a reply frame, checking it answers ``label``.

    Tolerant of what surrounds it on purpose. A reply arrives over a serial line
    that may have been mid-sentence when the read went out, so leading rubbish
    is skipped rather than treated as a failure, and a frame whose tail is
    missing still yields its data. What is not tolerated is a reply to a
    different question, because silently parsing one register's value as
    another's is the failure that would be hardest to notice.
    """
    start = reply.find(c.STX)
    if start < 0:
        raise ReplyError("no start of text in the sign's reply: %r" % reply)

    body = reply[start + 1 :]
    end = body.find(c.ETX)
    if end >= 0:
        body = body[:end]

    if not body.startswith(c.COMMAND_WRITE_SPECIAL):
        raise ReplyError(
            "the sign's reply does not begin with the special function command code: %r" % body
        )
    body = body[len(c.COMMAND_WRITE_SPECIAL) :]

    if not body.startswith(label):
        raise ReplyError(
            "asked the sign for %r and it answered about %r" % (label, body[:1])
        )
    return body[len(label) :]


def parse_general_information(reply: bytes) -> GeneralInformation:
    """Read the answer to ``F"`` into fields.

    The document gives the layout as ``FFFFFFFFfMmYyHhNnRSSPOOL,pool`` and its
    length as "28 or 29 ASCII characters", which is the whole difficulty: the
    revision letter is the one field that may not be there, and it sits in the
    middle. So the fixed parts are measured from both ends and the revision is
    whatever is left over, rather than being read at a fixed offset that would
    silently shift every field after it on a sign that omits it.
    """
    data = unwrap(reply, c.SF_GENERAL_INFORMATION)
    text = data.replace(c.NUL, b"").decode("latin-1").strip()

    comma = text.rfind(",")
    if comma < 0:
        raise ReplyError("no memory pool in the sign's reply: %r" % text)

    free = text[comma + 1 :]
    head = text[:comma]
    total = head[-4:]
    head = head[:-4]

    # From the right of what is left: two characters of speaker status, one of
    # time format, four of clock, four of release date. Eight of firmware
    # version from the left. Anything between is the revision letter.
    if len(head) < 19:
        raise ReplyError("the sign's reply is too short to be general information: %r" % text)

    speaker = head[-2:]
    time_format = head[-3]
    clock = head[-7:-3]
    released = head[-11:-7]
    version = head[:8]
    revision = head[8:-11]

    return GeneralInformation(
        firmware_version=version,
        firmware_revision=revision,
        firmware_released=_month_year(released),
        clock=_clock(clock),
        time_format=_time_format(time_format),
        # "00 = speaker enabled, FF = speaker disabled". Anything else is the
        # sign saying something this does not understand, and reporting that as
        # "enabled" would be a guess dressed as a fact.
        speaker_enabled=_speaker(speaker),
        memory_total=_hex(total, "memory pool size"),
        memory_free=_hex(free, "unused memory pool size"),
        raw=text,
    )


def _time_format(value: str) -> str:
    """Name the clock the sign draws, refusing a code that is neither.

    "S" and "M" are the only two the document defines. Reading anything else as
    12 hour would be the same guess dressed as a fact that :func:`_speaker`
    refuses, and this is troubleshooting output: a field invented to fill a gap
    is worse here than an error saying the gap is there.
    """
    upper = value.upper()
    if upper == c.TIME_FORMAT_24_HOUR.decode("ascii"):
        return "24 hour"
    if upper == c.TIME_FORMAT_12_HOUR.decode("ascii"):
        return "12 hour"
    raise ReplyError("the sign reported an unknown time format %r" % value)


def _speaker(value: str) -> bool:
    upper = value.upper()
    if upper == c.SPEAKER_ON.decode("ascii"):
        return True
    if upper == c.SPEAKER_OFF.decode("ascii"):
        return False
    raise ReplyError("the sign reported an unknown speaker status %r" % value)


def _hex(value: str, what: str) -> int:
    try:
        return int(value, 16)
    except ValueError as err:
        raise ReplyError("the sign reported %s as %r, which is not hex" % (what, value)) from err


def _month_year(value: str) -> str:
    """Render the four digit ``MmYy`` release date as ``MM/YY``.

    Not turned into a date. The year is two digits with no century, exactly as
    the sign's own date register is, and this project has already been bitten
    once by reading such a field as though it had one. See
    docs/protocol-notes.md.
    """
    if len(value) != 4 or not value.isdigit():
        raise ReplyError("the sign reported a release date of %r" % value)
    return "%s/%s" % (value[:2], value[2:])


def _clock(value: str) -> str:
    """Render the four digit ``HhNn`` time of day as ``HH:MM``."""
    if len(value) != 4 or not value.isdigit():
        raise ReplyError("the sign reported a time of %r" % value)
    return "%s:%s" % (value[:2], value[2:])
