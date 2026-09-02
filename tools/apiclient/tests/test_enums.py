"""Parsing an enumeration response, and holding what was loaded."""

from __future__ import annotations

import pytest

from apiclient import catalogue
from apiclient.enums import EnumStore, MalformedEnumeration, parse


def test_the_enumeration_shape_parses():
    entries = parse([{"name": "<red>", "description": "Set text colour to red"}])
    assert entries[0].name == "<red>"
    assert entries[0].description == "Set text colour to red"


def test_every_set_arrives_in_that_one_shape():
    for payload in (
        [{"name": "HOLD", "description": "hold"}],
        [{"name": "SOFT_RESET", "description": "reset"}],
        [{"name": "<red>", "description": "red"}],
    ):
        assert parse(payload)[0].description


def test_a_payload_that_is_not_a_list_is_refused():
    with pytest.raises(MalformedEnumeration):
        parse({"name": "<red>"})


def test_an_entry_without_a_name_is_refused_rather_than_left_blank():
    # A set of empty names looks exactly like a healthy one on screen, so this
    # has to be a refusal rather than a shrug.
    with pytest.raises(MalformedEnumeration) as caught:
        parse([{"token_text": "<red>", "description": "red"}])
    assert "token_text" in str(caught.value)


def test_a_missing_description_is_not_fatal():
    assert parse([{"name": "<red>"}])[0].description == ""


def test_nothing_is_loaded_until_it_is_loaded():
    store = EnumStore()
    for set_key in catalogue.SET_ORDER:
        assert not store.is_loaded(set_key)
        assert store.names(set_key) == ()


def test_loading_a_set_makes_its_names_available():
    store = EnumStore()
    entries = parse([{"name": "HOLD", "description": "hold"}])
    store.load(catalogue.DISPLAY_MODES, "GET /enumerations/display-modes", entries)
    assert store.is_loaded(catalogue.DISPLAY_MODES)
    assert store.names(catalogue.DISPLAY_MODES) == ("HOLD",)
    assert store.describe(catalogue.DISPLAY_MODES, "HOLD") == "hold"


def test_the_provenance_says_which_endpoint_answered():
    store = EnumStore()
    loaded = store.load(
        catalogue.MARKUP_TOKENS,
        "GET /enumerations/markup-tokens",
        parse([{"name": "<red>", "description": "red"}]),
    )
    assert "1 entry from GET /enumerations/markup-tokens at " in loaded.provenance()


def test_the_summary_keeps_the_endpoint_off_the_panel():
    # The summary has to fit the panel at its default width without wrapping. A
    # test with no Qt cannot measure that, so this pins the thing that pushed it
    # past one line: the endpoint, which belongs on the tooltip instead.
    store = EnumStore()
    loaded = store.load(
        catalogue.MARKUP_TOKENS,
        "GET /enumerations/markup-tokens",
        parse(
            [
                {"name": "<red>", "description": "red"},
                {"name": "<green>", "description": "green"},
            ]
        ),
    )
    assert loaded.summary().startswith("2 entries, loaded at ")
    assert "/enumerations" not in loaded.summary()


def test_loading_the_same_set_twice_replaces_it_rather_than_appending():
    store = EnumStore()
    store.load(catalogue.DISPLAY_MODES, "a", parse([{"name": "HOLD"}]))
    store.load(catalogue.DISPLAY_MODES, "b", parse([{"name": "ROTATE"}]))
    assert store.names(catalogue.DISPLAY_MODES) == ("ROTATE",)


def test_describing_something_in_a_set_that_was_never_loaded_is_empty_rather_than_a_crash():
    assert EnumStore().describe(catalogue.MARKUP_TOKENS, "<red>") == ""
