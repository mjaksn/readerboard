"""The catalogue is the client's claim to call every endpoint. This checks it.

``tests/test_constant_values.py`` exists in the service for the same reason:
a table that is only ever compared with itself will pass however wrong it is.
The comparison that matters here is against ``docs/openapi.json``, which is
generated from the routes and is diffed by CI, so a route added to the service
fails these tests in the same commit rather than a year later.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import REPO_ROOT

from relayclient import catalogue

OPENAPI = Path(REPO_ROOT) / "docs" / "openapi.json"


def described() -> set[tuple[str, str]]:
    """Return every (method, path) the checked-in OpenAPI description declares."""
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    return {
        (method.upper(), path)
        for path, operations in document["paths"].items()
        for method in operations
    }


def catalogued() -> set[tuple[str, str]]:
    """Return every (method, path) this client can call."""
    return {(operation.method, operation.path) for operation in catalogue.OPERATIONS}


def test_the_client_can_call_every_endpoint_the_service_describes():
    missing = described() - catalogued()
    assert not missing, "the service has endpoints this client cannot call: %s" % sorted(missing)


def test_the_client_invents_no_endpoint_the_service_does_not_have():
    extra = catalogued() - described()
    assert not extra, "this client offers endpoints the service does not have: %s" % sorted(extra)


def test_every_operation_id_is_unique():
    ids = [operation.id for operation in catalogue.OPERATIONS]
    assert len(ids) == len(set(ids))


def test_every_operation_lands_in_a_group_the_window_renders():
    for operation in catalogue.OPERATIONS:
        assert operation.group in catalogue.GROUP_ORDER


def test_grouping_keeps_every_operation():
    grouped = [operation for _name, bucket in catalogue.grouped() for operation in bucket]
    assert len(grouped) == len(catalogue.OPERATIONS)


def test_every_path_placeholder_has_an_input_to_fill_it():
    for operation in catalogue.OPERATIONS:
        names = {item.name for item in operation.path_inputs}
        placeholders = {
            part.split("}")[0]
            for part in operation.path.split("{")[1:]
        }
        assert placeholders == names, operation.id


def test_every_enumeration_set_has_at_least_one_endpoint_that_fills_it():
    for set_key in catalogue.SET_ORDER:
        assert catalogue.loaders_for(set_key), set_key


def test_every_field_enumeration_is_a_set_something_can_load():
    for operation in catalogue.OPERATIONS:
        for item in operation.body:
            if item.enum_set:
                assert item.enum_set in catalogue.SET_ORDER, (operation.id, item.name)


@pytest.mark.parametrize(
    ("set_key", "expected"),
    [
        (catalogue.MARKUP_TOKENS, 2),
        (catalogue.DISPLAY_MODES, 2),
        (catalogue.CONTROL_COMMANDS, 2),
        # The simple surface has no text positions endpoint, so this one can
        # only ever be filled from /v2. If that stops being true, this fails.
        (catalogue.TEXT_POSITIONS, 1),
    ],
)
def test_the_two_endpoint_families_fill_the_sets_they_are_expected_to(set_key, expected):
    assert len(catalogue.loaders_for(set_key)) == expected


def test_the_markup_fields_are_the_ones_that_take_markup():
    markup = {
        (operation.id, item.name)
        for operation in catalogue.OPERATIONS
        for item in operation.body
        if item.markup
    }
    assert markup == {
        ("put_message", "message"),
        ("post_alert", "message"),
        ("simple_write_message", "message"),
    }


def test_only_clearing_every_message_is_marked_destructive():
    destructive = {operation.id for operation in catalogue.OPERATIONS if operation.destructive}
    assert destructive == {"clear_messages"}


def test_health_is_the_only_operation_that_needs_no_key_and_writes_nothing():
    assert not catalogue.BY_ID["health"].needs_key


def test_every_write_carries_the_key():
    for operation_id in (
        "put_message",
        "delete_message",
        "clear_messages",
        "post_alert",
        "delete_alert",
        "sync_clock",
        "send_command",
        "simple_write_message",
        "simple_control_command",
    ):
        assert catalogue.BY_ID[operation_id].needs_key, operation_id


def test_no_read_asks_for_a_key_it_does_not_need():
    for operation_id in (
        "list_messages",
        "get_message",
        "get_alert",
        "health",
        "v2_markup_tokens",
        "simple_markup_tokens",
    ):
        assert not catalogue.BY_ID[operation_id].needs_key, operation_id
