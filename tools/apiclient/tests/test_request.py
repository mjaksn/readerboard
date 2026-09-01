"""Building a request, and the curl command that stands in for it."""

from __future__ import annotations

import json

import pytest

from apiclient import catalogue
from apiclient.request import (
    API_KEY_HEADER,
    InvalidRequest,
    as_curl,
    build,
    build_body,
    coerce,
    fill_path,
    normalise_base_url,
)

BASE = "http://127.0.0.1:8000"


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    assert normalise_base_url("http://host:8000/") == "http://host:8000"


def test_a_base_url_without_a_scheme_is_refused():
    with pytest.raises(InvalidRequest):
        normalise_base_url("127.0.0.1:8000")


def test_an_empty_base_url_is_refused():
    with pytest.raises(InvalidRequest):
        normalise_base_url("   ")


def test_a_path_parameter_is_substituted():
    operation = catalogue.BY_ID["get_message"]
    assert fill_path(operation, {"key": "kitchen"}) == "/messages/kitchen"


def test_a_path_parameter_is_percent_encoded():
    operation = catalogue.BY_ID["get_message"]
    assert fill_path(operation, {"key": "a b/c"}) == "/messages/a%20b%2Fc"


def test_an_empty_path_parameter_is_refused_because_the_url_would_collapse():
    operation = catalogue.BY_ID["get_message"]
    with pytest.raises(InvalidRequest):
        fill_path(operation, {"key": "   "})


def test_a_number_that_parses_is_sent_as_a_number():
    assert coerce("float", "12.5") == 12.5
    assert coerce("int", "3") == 3


def test_a_number_that_does_not_parse_is_sent_as_text_so_the_service_answers_for_it():
    assert coerce("float", "soon") == "soon"
    assert coerce("int", "many") == "many"


@pytest.mark.parametrize("text", ["nan", "NaN", "inf", "-inf", "Infinity"])
def test_a_float_with_no_json_spelling_is_sent_as_text_rather_than_as_itself(text):
    # float() takes all of these and json.dumps then writes a bare NaN or
    # Infinity, which JSON has no syntax for. Sent as text, the service answers
    # for the one field; sent as a float, it cannot read the document at all.
    assert coerce("float", text) == text


def test_no_body_this_client_builds_is_anything_but_json():
    prepared = build(
        catalogue.BY_ID["put_message"],
        BASE,
        path_values={"key": "kitchen"},
        body_values={"message": "hello", "ttl_seconds": "nan"},
    )
    assert prepared.body is not None
    assert "NaN" not in prepared.body
    assert json.loads(prepared.body)["ttl_seconds"] == "nan"


def test_an_empty_optional_field_is_left_out_rather_than_sent_as_null():
    operation = catalogue.BY_ID["put_message"]
    body = build_body(operation, {"message": "hello", "ttl_seconds": "", "source": ""})
    assert body is not None
    assert "ttl_seconds" not in body
    assert "source" not in body
    assert body["message"] == "hello"


def test_an_emptied_required_field_goes_out_empty_so_the_service_answers_for_it():
    # Clearing a required box is how you ask what the service does with an empty
    # one. Substituting the prefill would answer the question the client was
    # asked to put to the service.
    operation = catalogue.BY_ID["send_command"]
    body = build_body(operation, {"command": "", "parameter": "0930"})
    assert body == {"command": "", "parameter": "0930"}


def test_a_required_field_left_alone_still_sends_what_is_in_it():
    operation = catalogue.BY_ID["send_command"]
    body = build_body(operation, {"command": "SET_TIME", "parameter": "0930"})
    assert body == {"command": "SET_TIME", "parameter": "0930"}


def test_the_origin_is_the_address_a_request_actually_went_to():
    prepared = build(
        catalogue.BY_ID["get_message"], "http://pi.local:5001", path_values={"key": "k"}
    )
    assert prepared.origin == "http://pi.local:5001"
    assert prepared.url == "http://pi.local:5001/messages/k"


def test_an_operation_with_no_body_sends_none():
    assert build_body(catalogue.BY_ID["list_messages"], {}) is None


def test_a_write_carries_the_key_and_a_read_does_not():
    write = build(
        catalogue.BY_ID["put_message"],
        BASE,
        path_values={"key": "kitchen"},
        body_values={"message": "hi"},
        api_key="secret",
    )
    read = build(catalogue.BY_ID["list_messages"], BASE, api_key="secret")
    assert write.headers[API_KEY_HEADER] == "secret"
    assert API_KEY_HEADER not in read.headers


def test_the_body_is_json_the_service_would_accept():
    prepared = build(
        catalogue.BY_ID["put_message"],
        BASE,
        path_values={"key": "kitchen"},
        body_values={"message": "<red>hot", "display_mode": "HOLD", "order": "2"},
        api_key="secret",
    )
    assert prepared.body is not None
    assert json.loads(prepared.body) == {
        "message": "<red>hot",
        "display_mode": "HOLD",
        "order": 2,
    }
    assert prepared.url == "http://127.0.0.1:8000/messages/kitchen"
    assert prepared.method == "PUT"


def test_the_prepared_request_can_hand_back_headers_with_the_key_gone():
    prepared = build(
        catalogue.BY_ID["delete_alert"], BASE, api_key="hunter2"
    )
    assert prepared.headers[API_KEY_HEADER] == "hunter2"
    assert prepared.redacted_headers[API_KEY_HEADER] == "<redacted>"
    assert "hunter2" not in json.dumps(prepared.redacted_headers)


def test_curl_never_writes_the_key_into_the_command():
    command = as_curl(
        "POST",
        "http://host/alerts",
        {"Content-Type": "application/json", API_KEY_HEADER: "hunter2"},
        '{"message": "fire"}',
    )
    assert "hunter2" not in command
    assert '-H "X-API-Key: $READERBOARD_API_KEY"' in command


def test_curl_survives_a_body_containing_a_single_quote():
    command = as_curl("PUT", "http://host/x", {}, "it's fine")
    assert "'it'\\''s fine'" in command


def test_curl_reads_as_one_command():
    command = as_curl("GET", "http://host/health", {"Accept": "application/json"}, None)
    assert command.startswith("curl -X GET 'http://host/health'")
    assert "-d " not in command


def test_a_field_holding_only_a_space_is_sent_as_typed():
    # Typed on purpose, to see what the service makes of it. Rewriting it to ""
    # or dropping it answers a different question.
    operation = catalogue.BY_ID["put_message"]
    body = build_body(operation, {"message": " ", "source": " "})
    assert body is not None
    assert body["message"] == " "
    assert body["source"] == " "


def test_a_field_nobody_touched_is_still_left_out():
    operation = catalogue.BY_ID["put_message"]
    body = build_body(operation, {"message": "hello", "source": ""})
    assert body is not None
    assert "source" not in body
