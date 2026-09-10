"""Tests for reading what the sign says back.

These assert whole replies literally, the way ``tests/test_frames.py`` asserts
whole transmissions. When one fails, the parser is wrong, not the test.

The samples below are built from the document's own description of the reply
rather than copied from a sign, and that is worth stating plainly: they pin the
layout the document specifies. What a real BetaBrite answers has not been seen
yet, and if it disagrees, the document does not automatically win. This project
has already found four things the document promises that this hardware does not
do.
"""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol.replies import (
    ReplyError,
    parse_general_information,
    unwrap,
)


def framed(data: bytes, *, label: bytes = c.SF_GENERAL_INFORMATION) -> bytes:
    """Wrap a data field the way the sign wraps its answers.

    Twenty nulls, then the header, the Response type code, an address the
    document says is "sent regardless of the sign's actual address", and the
    **write** command code, which is what the sign echoes for a read.
    """
    return (
        c.NUL * 20
        + c.SOH
        + c.SIGN_TYPE_RESPONSE
        + c.SIGN_ADDRESS_BROADCAST
        + c.STX
        + c.COMMAND_WRITE_SPECIAL
        + label
        + data
        + c.ETX
    )


# The 29 character form, field by field: eight of firmware version, the
# revision letter B, released 01/93, the clock at 14:33, M for a 24 hour dial,
# 00 for a speaker that is enabled, a 4000H pool with 0BB8H of it free.
FULL = b"1044-160" b"B" b"0193" b"1433" b"M" b"00" b"4000" b"," b"0BB8"

# The 28 character form. The same sign with no revision letter reported.
NO_REVISION = b"1044-160" b"0193" b"1433" b"M" b"00" b"4000" b"," b"0BB8"


def test_the_samples_are_the_lengths_the_document_gives():
    """"28 or 29 ASCII characters", and both are exercised below."""
    assert len(FULL) == 29
    assert len(NO_REVISION) == 28


class TestUnwrap:
    def test_it_finds_the_data_field(self):
        assert unwrap(framed(b"HELLO"), c.SF_GENERAL_INFORMATION) == b"HELLO"

    def test_leading_noise_before_the_frame_is_skipped(self):
        # The line may have been mid-sentence when the read went out.
        assert unwrap(b"rubbish" + framed(b"HELLO"), c.SF_GENERAL_INFORMATION) == b"HELLO"

    def test_a_truncated_frame_still_yields_its_data(self):
        # No ETX, because the sign was still talking when the read gave up.
        cut = framed(b"HELLO").replace(c.ETX, b"")
        assert unwrap(cut, c.SF_GENERAL_INFORMATION) == b"HELLO"

    def test_an_answer_to_a_different_question_is_refused(self):
        # The one thing not tolerated. Parsing the run sequence as general
        # information would put confident nonsense in front of somebody
        # troubleshooting, which is worse than an error.
        reply = framed(b"HELLO", label=c.SF_SET_RUN_SEQUENCE)
        with pytest.raises(ReplyError, match="answered about"):
            unwrap(reply, c.SF_GENERAL_INFORMATION)

    def test_silence_is_refused(self):
        with pytest.raises(ReplyError, match="no start of text"):
            unwrap(c.NUL * 20, c.SF_GENERAL_INFORMATION)


class TestGeneralInformation:
    def test_every_field_of_a_full_reply(self):
        info = parse_general_information(framed(FULL))

        assert info.firmware_version == "1044-160"
        assert info.firmware_revision == "B"
        assert info.firmware_released == "01/93"
        assert info.clock == "14:33"
        assert info.time_format == "24 hour"
        assert info.speaker_enabled is True
        assert info.memory_total == 0x4000
        assert info.memory_free == 0x0BB8

    def test_a_missing_revision_letter_shifts_no_other_field(self):
        """The 28 character form, and the reason the fields are measured twice.

        The revision letter is the field that may be absent and it sits in the
        middle, so reading left to right at fixed offsets would shift every
        field after it and still parse. The damage would be quiet rather than
        loud: the release date would come out as 0193 shifted to 1930, a
        plausible wrong answer, and the clock would follow it.
        """
        info = parse_general_information(framed(NO_REVISION))

        assert info.firmware_revision == ""
        assert info.firmware_version == "1044-160"
        assert info.firmware_released == "01/93"
        assert info.clock == "14:33"
        assert info.memory_total == 0x4000
        assert info.memory_free == 0x0BB8

    def test_a_muted_sign_reads_as_muted(self):
        # The answer to "why did SOUND do nothing".
        assert parse_general_information(framed(FULL.replace(b"M00", b"MFF"))).speaker_enabled is False

    def test_a_twelve_hour_sign_says_so(self):
        reply = framed(FULL.replace(b"1433M", b"1433S"))
        assert parse_general_information(reply).time_format == "12 hour"

    def test_the_raw_answer_is_kept(self):
        # The document calls this troubleshooting information, and a field this
        # parser got wrong is exactly when somebody needs what actually arrived.
        assert parse_general_information(framed(FULL)).raw == FULL.decode("ascii")

    def test_an_unknown_speaker_status_is_refused_rather_than_guessed(self):
        # "00" and "FF" are the only two the document defines. Reporting
        # anything else as enabled would be a guess dressed as a fact.
        with pytest.raises(ReplyError, match="unknown speaker status"):
            parse_general_information(framed(FULL.replace(b"M00", b"MZZ")))

    def test_a_reply_with_no_pool_is_refused(self):
        with pytest.raises(ReplyError, match="no memory pool"):
            parse_general_information(framed(FULL.replace(b",", b"")))

    def test_a_short_reply_is_refused(self):
        with pytest.raises(ReplyError, match="too short"):
            parse_general_information(framed(b"1044,0BB8"))

    def test_a_non_hex_pool_is_refused(self):
        with pytest.raises(ReplyError, match="not hex"):
            parse_general_information(framed(FULL.replace(b",0BB8", b",ZZZZ")))

    def test_a_nonsense_clock_is_refused(self):
        with pytest.raises(ReplyError, match="reported a time"):
            parse_general_information(framed(FULL.replace(b"1433M", b"14:3M")))
