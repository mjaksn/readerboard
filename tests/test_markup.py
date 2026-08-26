"""Tests for the markup tokenizer, including the hang the old parser had."""

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


def test_the_home_assistant_payload_renders():
    # The exact shape Home_Assistant_Sign_REST_Commands.yaml sends, with the
    # template already filled in.
    rendered = render("<green>18.4<degree> <red><time>")
    assert rendered == (
        c.TEXT_COLOR_GREEN + b"18.4" + c.XC_DEGREES + b" " + c.TEXT_COLOR_RED + c.CURTIME_INSERT
    )


class TestUnterminatedTag:
    """The old parser looped forever on a '<' with no '>' after it.

    ``find('>', i)`` returned -1, the branch that advanced the cursor was
    skipped, and the request thread never came back. Both behaviours below are
    deliberate: reject it where we can, pass it through where we must.
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
        # This is what the old parser did, and compat payloads may rely on it.
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
        # The old implementation encoded the whole message as UTF-8, so this
        # produced two bytes that the sign rendered as garbage.
        assert render("é") == c.e_ACCENT
        assert render("é") != "é".encode()


def test_newline_becomes_a_carriage_return():
    assert render("a\nb") == b"a" + c.CR + b"b"
