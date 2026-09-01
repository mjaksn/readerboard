"""Reading responses back, including the one that fails while answering 200."""

from __future__ import annotations

import json

from relayclient import catalogue
from relayclient.format import Note, Section, Table, as_html, is_error, parse_body, render


def rendered_text(operation_id, status, body, reason=""):
    """Render a response and flatten it to text, which is what the assertions read."""
    result = render(catalogue.BY_ID[operation_id], status, reason, body)
    parts = [result.headline]
    for block in result.blocks:
        if isinstance(block, Note):
            parts.append(block.text)
        elif isinstance(block, Section):
            parts.extend("%s: %s" % (row.label, row.value) for row in block.rows)
        elif isinstance(block, Table):
            parts.extend(" | ".join(row) for row in block.rows)
    return result, "\n".join(parts)


# ===========================================================================
# The rule that matters
# ===========================================================================


def test_a_simple_surface_failure_is_an_error_even_though_it_answered_200():
    payload = {"result": "ERROR", "result_message": "The display mode 'X' is not valid"}
    assert is_error(200, payload)


def test_a_simple_surface_success_is_not_an_error():
    assert not is_error(200, {"result": "OK", "result_message": "Message displayed on sign"})


def test_a_status_code_of_four_hundred_or_more_is_an_error():
    assert is_error(404, {"detail": "no such slot"})
    assert is_error(503, None)


def test_a_request_that_never_completed_is_an_error():
    assert is_error(0, None)


def test_the_failed_simple_write_renders_as_a_failure_and_says_why_the_status_is_200():
    result, text = rendered_text(
        "simple_write_message",
        200,
        json.dumps({"result": "ERROR", "result_message": "The display mode 'X' is not valid"}),
    )
    assert result.ok is False
    assert "The display mode 'X' is not valid" in text
    assert "answers 200 whatever happens" in text


def test_the_successful_simple_write_renders_as_a_success():
    result, text = rendered_text(
        "simple_write_message",
        200,
        json.dumps({"result": "OK", "result_message": "Message displayed on sign"}),
    )
    assert result.ok is True
    assert "Message displayed on sign" in text


# ===========================================================================
# The shapes
# ===========================================================================


def test_health_is_broken_out_rather_than_dumped():
    body = json.dumps(
        {
            "status": "ok",
            "version": "0.2.0",
            "link": {
                "url": "socket://127.0.0.1:4001",
                "connected": True,
                "last_write_at": None,
                "last_error": None,
                "writes": 12,
                "suppressed_writes": 4,
            },
            "slots_used": 2,
            "slots_total": 8,
            "sign_in_sync": True,
            "alert_active": False,
            "clock_last_synced_at": None,
        }
    )
    result, text = rendered_text("health", 200, body)
    assert result.ok
    assert "socket://127.0.0.1:4001" in text
    assert "connected: yes" in text
    assert "slots used: 2 of 8" in text
    assert "{" not in text


def test_an_empty_message_list_says_so_rather_than_showing_an_empty_table():
    _result, text = rendered_text("list_messages", 200, "[]")
    assert "No messages are registered" in text


def test_a_message_list_becomes_a_table():
    body = json.dumps(
        [
            {
                "key": "kitchen",
                "label": "A",
                "message": "hello",
                "display_mode": "HOLD",
                "position": "MIDDLE",
                "order": 0,
                "source": "home assistant",
                "expires_at": None,
                "updated_at": "2026-08-31T07:00:00+00:00",
            }
        ]
    )
    result, text = rendered_text("list_messages", 200, body)
    assert "1 message sharing the sign" in text
    assert "kitchen" in text
    assert any(isinstance(block, Table) for block in result.blocks)


def test_no_alert_reads_as_a_sentence_rather_than_as_null():
    _result, text = rendered_text("get_alert", 200, "null")
    assert "No alert is holding the sign" in text


def test_a_204_reads_as_success_rather_than_as_an_empty_panel():
    result, text = rendered_text("delete_alert", 204, "")
    assert result.ok
    assert "returned no content" in text


def test_an_enumeration_renders_in_either_shape():
    v2 = json.dumps([{"name": "<red>", "description": "Set text colour to red"}])
    simple = json.dumps([{"token_text": "<red>", "description": "Set text colour to red"}])
    _v2_result, v2_text = rendered_text("v2_markup_tokens", 200, v2)
    _simple_result, simple_text = rendered_text("simple_markup_tokens", 200, simple)
    assert "<red>" in v2_text
    assert "<red>" in simple_text


# ===========================================================================
# Failures
# ===========================================================================


def test_a_plain_detail_error_shows_the_detail():
    result, text = rendered_text("get_message", 404, json.dumps({"detail": "no slot 'x'"}))
    assert result.ok is False
    assert "no slot 'x'" in text
    assert "no slot by that name" in result.headline


def test_a_validation_error_is_broken_out_field_by_field():
    body = json.dumps(
        {
            "detail": [
                {
                    "loc": ["body", "ttl_seconds"],
                    "msg": "Input should be greater than 0",
                    "type": "greater_than",
                }
            ]
        }
    )
    result, text = rendered_text("put_message", 422, body)
    assert result.ok is False
    assert "body -> ttl_seconds" in text
    assert "Input should be greater than 0" in text


def test_the_error_detail_carries_the_whole_body_for_the_dialog():
    body = json.dumps({"detail": "the sign is unreachable"})
    result, _text = rendered_text("put_message", 503, body)
    assert result.detail == body


def test_a_body_that_is_not_json_still_reaches_the_reader():
    result, text = rendered_text("health", 502, "<html>Bad Gateway</html>")
    assert result.ok is False
    assert "Bad Gateway" in text


def test_a_request_that_never_left_says_what_stopped_it():
    result, _text = rendered_text("health", 0, "", reason="Connection refused")
    assert result.ok is False
    assert "Connection refused" in result.headline


# ===========================================================================
# The HTML the panel actually shows
# ===========================================================================


def test_the_panel_never_shows_a_raw_json_object():
    body = json.dumps({"result": "OK", "result_message": "Message displayed on sign"})
    result = render(catalogue.BY_ID["simple_write_message"], 200, "OK", body)
    html = as_html(result)
    assert "Message displayed on sign" in html
    assert '{"result"' not in html


def test_markup_in_a_message_is_escaped_rather_than_rendered_as_html():
    body = json.dumps(
        {
            "key": "k",
            "label": "A",
            "message": "<red>hot",
            "display_mode": "HOLD",
            "position": "MIDDLE",
            "order": 0,
            "source": None,
            "expires_at": None,
            "updated_at": "2026-08-31T07:00:00+00:00",
        }
    )
    html = as_html(render(catalogue.BY_ID["get_message"], 200, "OK", body))
    assert "&lt;red&gt;hot" in html


def test_a_body_that_is_not_json_parses_as_nothing():
    assert parse_body("not json") is None
    assert parse_body("   ") is None
