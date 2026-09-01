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


def test_the_keyless_operations_are_exactly_the_reads():
    # Spelled out rather than sampled, because a note in the catalogue once
    # claimed health was the only endpoint needing no key and there are eleven.
    # Only the writes carry one; every read is open, which is what the service
    # documents.
    keyless = {operation.id for operation in catalogue.OPERATIONS if not operation.needs_key}
    assert keyless == {
        "health",
        "list_messages",
        "get_message",
        "get_alert",
        "v2_markup_tokens",
        "v2_display_modes",
        "v2_text_positions",
        "v2_control_commands",
        "simple_markup_tokens",
        "simple_display_modes",
        "simple_control_commands",
    }


def test_no_note_claims_health_is_the_only_keyless_endpoint():
    for operation in catalogue.OPERATIONS:
        assert "one endpoint that needs no" not in operation.note, operation.id


# ===========================================================================
# The fields, which the (method, path) diff above does not reach
#
# Without these, a field added to a request model would leave the client
# quietly unable to send it and nothing would fail. The comparison is
# structural on purpose: names, whether each is required, and which
# operations carry a body at all. Descriptions are not compared, because the
# catalogue's are placeholders written for a form and the schema's are written
# for an API reference, and making one mirror the other would be a decision
# about copy rather than a check of correctness.
# ===========================================================================


def schemas() -> dict:
    """Return the components/schemas block of the checked-in description."""
    return json.loads(OPENAPI.read_text(encoding="utf-8"))["components"]["schemas"]


def request_schema_name(method: str, path: str) -> str | None:
    """Return the schema a given operation's request body refers to, if it has one."""
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    operation = document["paths"][path][method.lower()]
    body = operation.get("requestBody")
    if body is None:
        return None
    ref = body["content"]["application/json"]["schema"]["$ref"]
    return ref.rsplit("/", 1)[-1]


def with_bodies():
    """Yield every catalogued operation the description gives a request body."""
    for operation in catalogue.OPERATIONS:
        name = request_schema_name(operation.method, operation.path)
        if name is not None:
            yield operation, schemas()[name]


def test_the_operations_with_a_body_are_the_ones_the_service_gives_one():
    described = {
        (operation.method, operation.path)
        for operation in catalogue.OPERATIONS
        if request_schema_name(operation.method, operation.path) is not None
    }
    catalogued_bodies = {
        (operation.method, operation.path)
        for operation in catalogue.OPERATIONS
        if operation.body
    }
    assert described == catalogued_bodies


def test_every_body_field_the_service_accepts_can_be_sent():
    for operation, schema in with_bodies():
        expected = set(schema.get("properties", {}))
        actual = {item.name for item in operation.body}
        assert actual == expected, operation.id


def test_the_required_fields_are_the_ones_the_service_requires():
    for operation, schema in with_bodies():
        expected = set(schema.get("required", []))
        actual = {item.name for item in operation.body if item.required}
        assert actual == expected, operation.id


def test_a_prefill_agrees_with_the_schema_default_wherever_there_is_one():
    for operation, schema in with_bodies():
        for item in operation.body:
            declared = schema.get("properties", {}).get(item.name, {})
            if "default" in declared:
                assert item.prefill == declared["default"], (operation.id, item.name)


def test_the_prefill_without_a_schema_default_is_this_client_s_own_convenience():
    # The one case, spelled out so that a second one has to be deliberate. The
    # service requires a display mode here and promises no default, so starting
    # the field on HOLD is the client's choice rather than the service's.
    unbacked = {
        (operation.id, item.name)
        for operation, schema in with_bodies()
        for item in operation.body
        if item.prefill is not None
        and "default" not in schema.get("properties", {}).get(item.name, {})
    }
    assert unbacked == {("simple_write_message", "display_mode")}


def test_a_number_field_is_typed_as_one():
    kinds = {"integer": "int", "number": "float"}
    for operation, schema in with_bodies():
        for item in operation.body:
            declared = schema.get("properties", {}).get(item.name, {})
            # An optional number arrives as anyOf [number, null], so look in
            # both places rather than only at the top level.
            options = [declared, *declared.get("anyOf", [])]
            wanted = {kinds[o["type"]] for o in options if o.get("type") in kinds}
            if wanted:
                assert item.kind in wanted, (operation.id, item.name, item.kind)


def test_every_path_parameter_the_service_declares_is_offered():
    document = json.loads(OPENAPI.read_text(encoding="utf-8"))
    for operation in catalogue.OPERATIONS:
        described = {
            parameter["name"]
            for parameter in document["paths"][operation.path][operation.method.lower()].get(
                "parameters", []
            )
            if parameter.get("in") == "path"
        }
        assert {item.name for item in operation.path_inputs} == described, operation.id
