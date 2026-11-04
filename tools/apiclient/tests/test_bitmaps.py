"""Parsing the icons' bitmaps, and the one thing about them the client knows."""

from __future__ import annotations

import pytest

from apiclient import catalogue
from apiclient.bitmaps import (
    COLOURS,
    MAX_COLUMNS,
    MAX_ROWS,
    STRAY,
    UNLIT_COLOUR,
    MalformedBitmap,
    colour_for,
    parse,
    tinted_tag,
)
from apiclient.enums import EnumStore

CHECK = {
    "name": "<icon:check>",
    "rows": ["...", "..#", "##."],
    "inks": {"#": "green"},
}


def test_the_bitmap_shape_parses():
    bitmap = parse([CHECK])["<icon:check>"]
    assert bitmap.rows == ("...", "..#", "##.")
    assert (bitmap.width, bitmap.height) == (3, 3)


def test_a_dot_is_painted_in_the_colour_its_ink_names():
    bitmap = parse([CHECK])["<icon:check>"]
    assert bitmap.colour_at(2, 1) == COLOURS["green"]


def test_an_unlit_dot_is_painted_as_one_rather_than_left_out():
    bitmap = parse([CHECK])["<icon:check>"]
    assert bitmap.colour_at(0, 0) == UNLIT_COLOUR


def test_a_colour_word_with_no_shade_is_conspicuous_rather_than_unlit():
    # The service may grow an ink this client has never heard of. Painting it
    # as an unlit dot would put a hole in the icon that reads as part of it.
    assert colour_for("ultraviolet") == STRAY
    bitmap = parse([{**CHECK, "inks": {"#": "ultraviolet"}}])["<icon:check>"]
    assert bitmap.colour_at(2, 1) == STRAY


def test_every_colour_the_service_can_name_has_a_shade():
    # Read from the service at test time only. The client never imports it,
    # which is the rule; this is what notices the two drifting apart.
    from readerboard import icons

    assert set(COLOURS) == set(icons.TINT_INKS)


def test_the_size_limit_is_the_protocols_own():
    # Read from the service at test time, as above. A limit tighter than the
    # protocol's would refuse a picture a sign could hold.
    from readerboard.protocol import constants

    assert (MAX_ROWS, MAX_COLUMNS) == (constants.DOTS_MAX_ROWS, constants.DOTS_MAX_COLUMNS)


def test_a_payload_that_is_not_a_list_is_refused():
    with pytest.raises(MalformedBitmap):
        parse(CHECK)


@pytest.mark.parametrize("missing", ["name", "rows", "inks"])
def test_a_bitmap_missing_a_field_is_refused(missing):
    item = {key: value for key, value in CHECK.items() if key != missing}
    with pytest.raises(MalformedBitmap, match=missing):
        parse([item])


def test_ragged_rows_are_refused_rather_than_drawn_skewed():
    with pytest.raises(MalformedBitmap, match="one width"):
        parse([{**CHECK, "rows": ["...", "..", "##."]}])


@pytest.mark.parametrize(
    "rows",
    [["." * (MAX_COLUMNS + 1)] * 3, ["..."] * (MAX_ROWS + 1)],
    ids=["too wide", "too high"],
)
def test_a_bitmap_bigger_than_any_picture_file_is_refused_before_it_is_painted(rows):
    with pytest.raises(MalformedBitmap, match="no picture file holds more"):
        parse([{**CHECK, "rows": rows}])


def test_a_bitmap_as_big_as_a_picture_file_can_be_is_drawn():
    assert parse([{**CHECK, "rows": ["." * MAX_COLUMNS] * MAX_ROWS}])


def test_no_rows_at_all_is_refused():
    with pytest.raises(MalformedBitmap, match="no rows"):
        parse([{**CHECK, "rows": []}])


def test_a_lit_dot_whose_colour_nobody_said_is_refused():
    # Guessing would be knowing a vocabulary nobody told the client.
    with pytest.raises(MalformedBitmap, match="'R'"):
        parse([{**CHECK, "rows": ["R..", "..#", "##."]}])


def test_an_ink_that_is_not_one_character_is_refused():
    with pytest.raises(MalformedBitmap):
        parse([{**CHECK, "inks": {"##": "green"}}])


def test_the_store_keeps_bitmaps_apart_from_the_set_they_draw():
    store = EnumStore()
    store.load_bitmaps(catalogue.ICONS, "GET /enumerations/icons/bitmaps", parse([CHECK]))
    assert not store.is_loaded(catalogue.ICONS)
    loaded = store.bitmaps(catalogue.ICONS)
    assert loaded is not None
    assert list(loaded.by_name) == ["<icon:check>"]
    assert loaded.summary().startswith("1 bitmap, loaded at ")


def test_nothing_is_drawn_until_the_bitmaps_are_loaded():
    assert EnumStore().bitmaps(catalogue.ICONS) is None


# ===========================================================================
# Tints
# ===========================================================================

SUN = {"name": "<icon:sun>", "rows": ["o.y"], "inks": {"o": "orange", "y": "yellow"}}


def test_an_icon_with_a_tint_ink_takes_a_tint_and_one_without_does_not():
    drawn = parse([CHECK, SUN])
    assert drawn["<icon:check>"].takes_tint
    assert not drawn["<icon:sun>"].takes_tint


def test_a_tint_repaints_the_tint_ink_and_nothing_else():
    check = parse([CHECK])["<icon:check>"].tinted("red")
    assert check.colour_at(2, 1) == COLOURS["red"]
    assert check.colour_at(0, 0) == UNLIT_COLOUR


def test_a_tint_leaves_an_icon_in_fixed_colours_as_it_is():
    # The service refuses a tint on one, so showing it tinted would be showing
    # something no message can draw.
    sun = parse([SUN])["<icon:sun>"]
    assert sun.tinted("red") is sun


def test_no_tint_is_the_icon_as_listed():
    check = parse([CHECK])["<icon:check>"]
    assert check.tinted(None) is check


def test_tinting_does_not_change_the_bitmap_it_was_made_from():
    check = parse([CHECK])["<icon:check>"]
    check.tinted("red")
    assert check.inks == {"#": "green"}


def test_the_tinted_tag_puts_the_colour_inside_the_bracket():
    assert tinted_tag("<icon:check>", "red") == "<icon:check:red>"


def test_no_tint_leaves_the_tag_alone():
    assert tinted_tag("<icon:check>", None) == "<icon:check>"


def test_a_name_that_is_not_a_tag_is_left_alone_rather_than_mangled():
    assert tinted_tag("check", "red") == "check"
