"""Annotation has to split a body the way the sign reads it, not byte by byte."""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol.markup import render
from readerboard.protocol.tokens import MARKUP_TOKENS
from signsim.spans import SpanKind, annotate, readable


@pytest.mark.parametrize("token", MARKUP_TOKENS, ids=lambda token: token.text)
def test_every_markup_token_annotates_as_one_span(token):
    spans = annotate(token.value)
    assert len(spans) == 1
    assert spans[0].label == token.text
    assert spans[0].data == token.value
    assert spans[0].description == token.description


class TestLongestMatch:
    def test_a_three_byte_attribute_is_not_read_as_two(self):
        spans = annotate(c.CHAR_ATTRIB_DBLH_ON)
        assert len(spans) == 1
        assert spans[0].data == c.CHAR_ATTRIB_DBLH_ON

    def test_a_colour_followed_by_text_is_two_spans(self):
        spans = annotate(c.TEXT_COLOR_RED + b"HI")
        assert [one.kind for one in spans] == [SpanKind.CONTROL, SpanKind.TEXT]
        assert spans[1].data == b"HI"

    def test_consecutive_printable_bytes_group_into_one_span(self):
        spans = annotate(b"HELLO THERE")
        assert len(spans) == 1
        assert spans[0].kind is SpanKind.TEXT


class TestOffsets:
    def test_offsets_point_back_into_the_payload(self):
        spans = annotate(c.TEXT_COLOR_RED + b"HI", offset=5)
        assert spans[0].offset == 5
        assert spans[1].offset == 7
        assert spans[1].end == 9

    def test_every_byte_is_covered_exactly_once(self):
        body = render("<red>HI <degree>F<flash_on>!<flash_off>")
        spans = annotate(body)
        assert b"".join(one.data for one in spans) == body


class TestKinds:
    def test_a_tilde_is_a_half_space_rather_than_literal_text(self):
        # The sign draws 0x7E as half a space, so calling it a tilde would be a
        # lie however printable the byte looks.
        spans = annotate(b"~")
        assert spans[0].kind is SpanKind.GLYPH
        assert spans[0].label == "<half_space>"

    def test_an_extended_character_is_a_glyph(self):
        spans = annotate(c.DEGREES)
        assert spans[0].kind is SpanKind.GLYPH

    def test_a_byte_in_no_table_is_unknown(self):
        spans = annotate(b"\xf0")
        assert spans[0].kind is SpanKind.UNKNOWN
        assert spans[0].label == "0xF0"


class TestVariableLengthSequences:
    def test_a_string_file_insert_takes_its_filename_with_it(self):
        spans = annotate(c.STRING_FILE_INSERT + b"Q" + b"HI")
        assert spans[0].data == c.STRING_FILE_INSERT + b"Q"
        assert spans[1].data == b"HI"

    def test_a_mid_body_mode_change_reads_position_and_mode(self):
        spans = annotate(c.SOM + c.TEXT_POS_FILL + c.MODE_ROTATE + b"HI")
        assert spans[0].label == "mode change"
        assert spans[0].data == c.SOM + c.TEXT_POS_FILL + c.MODE_ROTATE
        assert spans[1].data == b"HI"

    def test_a_two_byte_mode_in_a_mid_body_mode_change(self):
        spans = annotate(c.SOM + c.TEXT_POS_MIDDLE + c.MODE_STARBURST + b"HI")
        assert spans[0].data == c.SOM + c.TEXT_POS_MIDDLE + c.MODE_STARBURST
        assert spans[1].data == b"HI"


class TestReadable:
    def test_markup_survives_the_round_trip(self):
        written = "<red>HI <degree>F<flash_on>!<flash_off>"
        assert readable(annotate(render(written))) == written

    def test_an_extended_character_reads_back_as_itself(self):
        assert readable(annotate(render("café"))) == "café"

    def test_an_unknown_byte_is_shown_as_an_escape(self):
        assert readable(annotate(b"\xf0")) == "\\xf0"
