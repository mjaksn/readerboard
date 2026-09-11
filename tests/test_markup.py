"""Tests for the markup tokenizer, including the unterminated-tag hang."""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol.markup import (
    THIN_SPACE,
    MarkupError,
    references,
    render,
    render_value,
)
from readerboard.protocol.tokens import DISPLAY_MODES, MARKUP_TOKENS


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


def test_no_mode_claims_a_specifier_the_sign_falls_back_from():
    """The gap between the two special mode tables is empty, and was measured.

    Table 66 runs its specifiers to "C" and Table 67 starts at "S", skipping
    "T". Nothing says what lives between them, and after 64H turned out to be a
    real mode under the word "reserved", the gap looked worth sweeping.

    All sixteen went to the sign on 2026-09-10 and every one drew INTERLOCK.
    That is a fallback rather than a refusal: the sign accepted each write and
    displayed the message in a mode nobody asked for.

    Pinned as an absence for the same reason the date and double height ones
    are. Offering one of these would look like filling a gap, and would in fact
    be a second name for INTERLOCK.
    """
    offered = {token.value for token in DISPLAY_MODES}
    gap = [b"n" + bytes([code]) for code in [*range(ord("D"), ord("S")), ord("T")]]

    assert len(gap) == 16
    for value in gap:
        assert value not in offered, "%r draws INTERLOCK on this sign" % value

    # The thing they all fall back to is offered under its own name, which is
    # what makes any of them redundant rather than merely undocumented.
    assert c.MODE_INTERLOCK in offered


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


class TestTheCharactersReadOffThePrintedTable:
    """The ten added once the document's Character column was read as an image.

    Each was drawn on the sign before being mapped. They are asserted one at a
    time rather than in a loop over ``EXTENDED_CHARACTERS``, because a loop over
    the map compares the map to itself and would pass with every byte in it
    wrong. That is the same shape of mistake as comparing the token table to
    itself, which ``tests/test_constant_values.py`` exists to catch.
    """

    def test_the_accented_capitals_a_message_in_spanish_needs(self):
        # These are the ones that made the service answer "the sign cannot
        # display 'Á'" while talking to a sign that could.
        assert render("Á") == c.A_ACCENT
        assert render("Ê") == c.E_ACCENT_HAT
        assert render("Í") == c.I_ACCENT

    def test_a_whole_word_that_used_to_be_refused(self):
        assert render("ÁGUILA") == c.A_ACCENT + b"GUILA"

    def test_the_currency_and_ordinal_marks(self):
        assert render("₧") == c.PESETA
        assert render("ƒ") == c.SLANT_F
        assert render("1ª") == b"1" + c.SUPER_a
        assert render("1º") == b"1" + c.SUPER_o

    def test_the_theta_pair(self):
        assert render("θ") == c.theta
        assert render("Θ") == c.THETA

    def test_the_thin_space_is_narrower_than_the_half_space(self):
        # Three widths of space reach the sign: an ordinary ASCII one, the half
        # space the <half_space> token sends, and this one. All three are
        # different bytes, which is the whole reason for offering it.
        assert render(THIN_SPACE) == c.SINGLE_COL_SPACE
        assert c.SINGLE_COL_SPACE != c.HALF_SPACE
        assert c.SINGLE_COL_SPACE != b" "

    def test_an_ordinary_space_is_still_an_ordinary_space(self):
        # THIN_SPACE is U+2009 and an ASCII space is U+0020. Mapping the first
        # must not disturb the second, which every message contains.
        assert render("a b") == b"a b"

    def test_the_ones_deliberately_left_out_are_still_refused(self):
        # B0H to B9H and the diacritics at BBH to BDH were not readable at five
        # by seven and are not guessed at. Strict refusal is the correct answer
        # for them: a wrong mapping would be accepted and silently drawn.
        for character in ("š", "ž", "Đ", "Â"):
            with pytest.raises(MarkupError, match="cannot display"):
                render(character)


def test_newline_becomes_a_carriage_return():
    assert render("a\nb") == b"a" + c.CR + b"b"


class TestVariableCalls:
    """<var:name> becomes 10H and the label of the STRING file the name lives in."""

    def test_a_call_becomes_the_call_string_code_and_the_label(self):
        built = render("T=<var:temp>F", variables={"temp": b"a"})
        assert built == b"T=" + c.STRING_FILE_INSERT + b"a" + b"F"

    def test_two_calls_to_one_variable_call_it_twice(self):
        built = render("<var:n> and <var:n>", variables={"n": b"b"})
        assert built == b"\x10b and \x10b"

    def test_a_call_sits_among_tokens(self):
        built = render("<fixed_width><var:temp><degree>", variables={"temp": b"a"})
        assert built == c.FIXED_WIDTH_ON + b"\x10a" + c.XC_DEGREES

    def test_an_unknown_name_is_refused_when_strict(self):
        with pytest.raises(MarkupError, match="no variable named 'temp'"):
            render("<var:temp>", variables={})

    def test_no_map_at_all_is_a_different_refusal(self):
        # None means nothing here can call a variable, which is not the same as
        # a variable that does not exist yet, and the caller is told which.
        with pytest.raises(MarkupError, match="cannot be used here"):
            render("<var:temp>")

    @pytest.mark.parametrize("name", ["Temp", "x" * 33, "a:b"])
    def test_a_name_outside_the_grammar_is_refused(self, name):
        with pytest.raises(MarkupError, match="not a variable name"):
            render("<var:%s>" % name, variables={name: b"a"})

    def test_an_empty_name_is_refused(self):
        with pytest.raises(MarkupError, match="not a variable name"):
            render("<var:>", variables={})

    def test_leniently_an_unknown_name_renders_nothing(self):
        # Not the literal tag, which is what an unknown token would get. The sign
        # draws a call to a missing STRING as nothing at all, so a restored
        # message whose variable has gone reads the way the sign would show it.
        assert render("[<var:gone>]", strict=False, variables={}) == b"[]"

    def test_leniently_no_map_renders_nothing(self):
        assert render("[<var:temp>]", strict=False) == b"[]"

    def test_a_colon_in_an_unknown_tag_is_still_an_unknown_tag(self):
        with pytest.raises(MarkupError, match="unknown markup token"):
            render("<12:30>")
        assert render("<12:30>", strict=False) == b"<12:30>"


class TestReferences:
    def test_names_come_back_once_each_in_order(self):
        assert references("<var:b> <var:a> <var:b>") == ["b", "a"]

    def test_a_message_with_no_calls_references_nothing(self):
        assert references("<red>plain < text>") == []

    def test_an_invalid_name_is_still_reported(self):
        assert references("<var:Temp>") == ["Temp"]

    def test_references_and_render_agree_on_what_a_tag_is(self):
        # "<var:a b>" is not a tag, because a space ends one, so neither
        # function may treat it as a call.
        assert references("<var:a b>") == []
        assert render("<var:a b>", strict=False) == b"<var:a b>"


class TestValues:
    """A value is the message language less what the sign cannot draw in a STRING."""

    def test_plain_text(self):
        assert render_value("72") == b"72"

    def test_formatting_is_allowed(self):
        assert render_value("<red>DOWN") == c.TEXT_COLOR_RED + b"DOWN"

    def test_what_the_document_leaves_out_but_the_sign_draws_is_allowed(self):
        # Rainbow, flash, the 1DH attributes and the extended characters are
        # all missing from Table 18 and all drew correctly inside a STRING.
        built = render_value("<rainbow1><flash_on><bold_on>72<degree>°")
        assert built == (
            c.TEXT_COLOR_RAINBOW1
            + c.CHAR_FLASH_ON
            + c.CHAR_ATTRIB_WIDE_ON
            + b"72"
            + c.XC_DEGREES
            + c.DEGREES
        )

    def test_the_day_of_week_is_refused(self):
        # Measured: the sign drops 0BH inside a STRING and draws the selector,
        # so this would show a 9.
        with pytest.raises(MarkupError, match="literal character"):
            render_value("<week_day>")

    def test_every_date_insert_the_markup_offers_is_refused(self):
        for token in MARKUP_TOKENS:
            if token.value.startswith(b"\x0b"):
                with pytest.raises(MarkupError):
                    render_value(token.text)

    def test_a_value_cannot_call_another_variable(self):
        # Measured: the sign draws the call as the label's letter.
        with pytest.raises(MarkupError, match="cannot call another variable"):
            render_value("<var:other>")

    def test_leniently_the_refused_ones_render_nothing(self):
        assert render_value("a<week_day>b<var:x>c", strict=False) == b"abc"

    def test_the_time_is_allowed(self):
        assert render_value("<time>") == c.CURTIME_INSERT
