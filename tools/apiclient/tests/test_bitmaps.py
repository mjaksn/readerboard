"""Parsing the icons' bitmaps, and the one thing about them the client knows."""

from __future__ import annotations

import pytest

from apiclient import catalogue
from apiclient.bitmaps import (
    COLOURS,
    STRAY,
    UNLIT_COLOUR,
    MalformedBitmap,
    colour_for,
    parse,
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
