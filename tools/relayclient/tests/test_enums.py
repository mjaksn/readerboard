"""Normalising the two enumeration shapes, and holding what was loaded."""

from __future__ import annotations

import pytest

from relayclient import catalogue
from relayclient.enums import EnumStore, MalformedEnumeration, parse


def test_the_v2_shape_parses():
    entries = parse([{"name": "<red>", "description": "Set text colour to red"}], "name")
    assert entries[0].name == "<red>"
    assert entries[0].description == "Set text colour to red"


def test_the_simple_shape_parses_to_exactly_the_same_thing():
    v2 = parse([{"name": "<red>", "description": "red"}], "name")
    simple = parse([{"token_text": "<red>", "description": "red"}], "token_text")
    assert v2 == simple


def test_each_simple_endpoint_names_its_entries_differently_and_all_of_them_work():
    for name_field, payload in (
        ("display_mode", [{"display_mode": "HOLD", "description": "hold"}]),
        ("control_command", [{"control_command": "SOFT_RESET", "description": "reset"}]),
        ("token_text", [{"token_text": "<red>", "description": "red"}]),
    ):
        assert parse(payload, name_field)[0].description


def test_a_payload_that_is_not_a_list_is_refused():
    with pytest.raises(MalformedEnumeration):
        parse({"name": "<red>"}, "name")


def test_an_entry_without_the_expected_name_field_is_refused_rather_than_left_blank():
    with pytest.raises(MalformedEnumeration) as caught:
        parse([{"token_text": "<red>", "description": "red"}], "name")
    assert "token_text" in str(caught.value)


def test_a_missing_description_is_not_fatal():
    assert parse([{"name": "<red>"}], "name")[0].description == ""


def test_nothing_is_loaded_until_it_is_loaded():
    store = EnumStore()
    for set_key in catalogue.SET_ORDER:
        assert not store.is_loaded(set_key)
        assert store.names(set_key) == ()


def test_loading_a_set_makes_its_names_available():
    store = EnumStore()
    entries = parse([{"name": "HOLD", "description": "hold"}], "name")
    store.load(catalogue.DISPLAY_MODES, "GET /v2/enumerations/display-modes", entries)
    assert store.is_loaded(catalogue.DISPLAY_MODES)
    assert store.names(catalogue.DISPLAY_MODES) == ("HOLD",)
    assert store.describe(catalogue.DISPLAY_MODES, "HOLD") == "hold"


def test_the_provenance_says_which_endpoint_answered():
    store = EnumStore()
    loaded = store.load(
        catalogue.MARKUP_TOKENS,
        "GET /Enumerations/MarkupTokens",
        parse([{"token_text": "<red>", "description": "red"}], "token_text"),
    )
    assert "1 from GET /Enumerations/MarkupTokens" in loaded.summary()


def test_loading_the_same_set_twice_replaces_it_rather_than_appending():
    store = EnumStore()
    store.load(catalogue.DISPLAY_MODES, "a", parse([{"name": "HOLD"}], "name"))
    store.load(catalogue.DISPLAY_MODES, "b", parse([{"name": "ROTATE"}], "name"))
    assert store.names(catalogue.DISPLAY_MODES) == ("ROTATE",)


def test_describing_something_in_a_set_that_was_never_loaded_is_empty_rather_than_a_crash():
    assert EnumStore().describe(catalogue.MARKUP_TOKENS, "<red>") == ""
