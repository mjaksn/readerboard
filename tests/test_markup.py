"""Tests for the markup tokenizer, including the unterminated-tag hang."""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol.markup import MarkupError, render
from readerboard.protocol.tokens import MARKUP_TOKENS


def test_plain_text_is_ascii():
    assert render("HELLO") == b"HELLO"


def test_empty_message_renders_to_nothing():
    assert render("") == b""


def test_known_token_becomes_its_bytes():
    assert render("<red>HI") == c.TEXT_COLOR_RED + b"HI"


def test_every_token_in_the_table_renders():
    for token in MARKUP_TOKENS:
        assert render(token.text) == token.value


def test_no_token_inserts_the_signs_date():
    # This sign puts no century on its two-digit year, so a date token would
    # render a confidently wrong date. Pinned as an absence, because the codes
    # are still in constants.py and re-adding a token for one would otherwise
    # look like filling a gap. tokens.py has the reasoning.
    date_values = {
        c.CURDATE_MMDDYY_SLASH,
        c.CURDATE_DDMMYY_SLASH,
        c.CURDATE_MMDDYY_DASH,
        c.CURDATE_DDMMYY_DASH,
        c.CURDATE_MMDDYY_DOT,
        c.CURDATE_DDMMYY_DOT,
        c.CURDATE_MMDDYY_SPACE,
        c.CURDATE_DDMMYY_SPACE,
        c.CURDATE_MMMDDYYYY,
    }
    offered = {token.value for token in MARKUP_TOKENS}
    assert not (offered & date_values)


def test_no_token_claims_to_double_the_character_height():
    # A Betabrite is seven pixels high, and on seven rows both the 05H form and
    # the 1DH+2 attribute drew text identical to plain. Pinned as an absence for
    # the same reason the date one is: the constants are still there.
    offered = {token.value for token in MARKUP_TOKENS}
    assert c.DBL_HEIGHT_CHARS_ON not in offered
    assert c.DBL_HEIGHT_CHARS_OFF not in offered
    assert c.CHAR_ATTRIB_DBLH_ON not in offered


def test_the_looks_that_survived_the_sign_are_offered():
    # Twenty character sets and attributes were put on the sign and collapsed
    # into these. Anything not here rendered identically to plain text, so it
    # would be a second name for nothing.
    offered = {token.value for token in MARKUP_TOKENS}
    for value in (
        c.CHARSET_7_NORMAL,
        c.CHARSET_5_NORMAL,
        c.CHARSET_7_FANCY,
        c.CHAR_ATTRIB_WIDE_ON,
        c.CHAR_ATTRIB_DBLW_ON,
    ):
        assert value in offered


def test_no_token_offers_a_look_the_sign_draws_as_plain_text():
    # Measured on the sign: these render pixel-identical to no markup at all.
    offered = {token.value for token in MARKUP_TOKENS}
    for value in (
        c.CHARSET_10_NORMAL,
        c.CHAR_ATTRIB_FNCY_ON,
        c.CHAR_ATTRIB_DESC_ON,
        c.WIDE_CHARS_ON,
        c.WIDE_CHARS_OFF,
    ):
        assert value not in offered


def test_the_time_and_day_of_week_are_still_offered():
    # Both are registers the clock sync writes, so unlike the date they are
    # right. Removing the date tokens must not take these with them.
    offered = {token.value for token in MARKUP_TOKENS}
    assert c.CURTIME_INSERT in offered
    assert c.CURDATE_WEEKDAYY in offered


def test_the_home_assistant_payload_renders():
    # A temperature and the time, the common shape: a value from somewhere
    # else, then <time> for the sign to fill in on its own.
    rendered = render("<green>18.4<degree> <red><time>")
    assert rendered == (
        c.TEXT_COLOR_GREEN + b"18.4" + c.XC_DEGREES + b" " + c.TEXT_COLOR_RED + c.CURTIME_INSERT
    )


class TestUnterminatedTag:
    """A '<' with no '>' after it must not hang the tokenizer.

    The obvious implementation searches for the closing bracket and advances the
    cursor only when it finds one, so an unterminated tag leaves the cursor
    where it was and the loop spins forever, taking the thread with it. Both
    behaviours below are deliberate: reject it where we can, pass it through
    where we must.
    """

    def test_strict_rejects(self):
        with pytest.raises(MarkupError, match="unterminated tag"):
            render("a < b")

    def test_lenient_passes_it_through(self):
        assert render("a < b", strict=False) == b"a < b"

    def test_a_lone_bracket_at_the_end_terminates(self):
        assert render("done<", strict=False) == b"done<"

    def test_bracket_run_terminates(self):
        assert render("<<<<", strict=False) == b"<<<<"


class TestUnknownTag:
    def test_strict_rejects(self):
        with pytest.raises(MarkupError, match="unknown markup token"):
            render("<nosuchtag>")

    def test_lenient_emits_it_literally(self):
        # Lenient mode passes an unknown tag through as literal text, so a
        # message written for a newer token set still displays something.
        assert render("<nosuchtag>", strict=False) == b"<nosuchtag>"


def test_empty_brackets_are_not_a_tag():
    assert render("<>", strict=False) == b"<>"


def test_text_with_spaces_inside_brackets_is_not_a_tag():
    assert render("a <b c> d", strict=False) == b"a <b c> d"


class TestNonAscii:
    def test_a_mappable_character_uses_the_signs_own_byte(self):
        assert render("café") == b"caf" + c.e_ACCENT

    def test_the_degree_sign_maps(self):
        assert render("18°") == b"18" + c.DEGREES

    def test_strict_rejects_a_character_the_sign_cannot_show(self):
        with pytest.raises(MarkupError, match="cannot display"):
            render("hello \U0001f600")

    def test_lenient_replaces_it(self):
        assert render("hello \U0001f600", strict=False) == b"hello ?"

    def test_utf8_is_not_emitted_raw(self):
        # Encoding the message as UTF-8 would put two bytes on the wire here,
        # which the sign renders as garbage rather than as an accented letter.
        assert render("é") == c.e_ACCENT
        assert render("é") != "é".encode()


def test_newline_becomes_a_carriage_return():
    assert render("a\nb") == b"a" + c.CR + b"b"
