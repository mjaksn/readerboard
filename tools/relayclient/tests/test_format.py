"""Reading responses back, including the one that fails while answering 200."""

from __future__ import annotations

import json

from relayclient import catalogue
from relayclient.format import (
    UNREADABLE,
    Note,
    Section,
    Table,
    as_html,
    is_error,
    parse_body,
    render,
)


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


def test_a_404_also_offers_the_reading_that_the_route_is_missing():
    # A client pointed at an older service gets a 404 for an endpoint that
    # service never had. Naming only the slot reading would send the reader
    # looking for the wrong thing.
    result, _text = rendered_text("v2_text_positions", 404, json.dumps({"detail": "Not Found"}))
    assert "no such route on this service" in result.headline


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


def test_the_two_ways_of_arriving_at_no_value_stay_apart():
    # A body of "null" and a body that is not JSON both leave nothing to read,
    # and they mean opposite things. The service really does answer null, so
    # they cannot share a return value.
    assert parse_body("not json") is UNREADABLE
    assert parse_body("null") is None
    assert parse_body("   ") is None


def test_a_body_that_is_not_json_is_a_failure_rather_than_a_quiet_success():
    # Something answered on the address, but not this service: a proxy error
    # page, a captive portal, the wrong application on the right port. Read as
    # "no value", the alert formatter states that the sign is rotating normally,
    # in green, on the strength of a response that said nothing of the kind.
    result, text = rendered_text("get_alert", 200, "<html>x</html>", reason="OK")
    assert result.ok is False
    assert "No alert is holding the sign" not in text
    assert "the call succeeded" not in result.headline
    assert "not JSON" in result.headline


def test_a_body_that_is_a_literal_null_still_means_what_it_says():
    # GET /v2/alerts answers null when nothing is holding the sign, and its
    # schema says so. Reading the unreadable body as null is the bug above;
    # reading null as unreadable would break the endpoint's own answer, so this
    # is the half of the pair that stops the fix overshooting.
    result, text = rendered_text("get_alert", 200, "null", reason="OK")
    assert result.ok is True
    assert "No alert is holding the sign" in text


def test_a_simple_surface_failure_is_not_headlined_as_a_success():
    # The headline is built from the status code, and 200 means "the call
    # succeeded". Saying that above a response this module has just decided is
    # a failure would be the exact mistake the module exists to prevent, made
    # in its own first line.
    result, _text = rendered_text(
        "simple_write_message",
        200,
        json.dumps({"result": "ERROR", "result_message": "the sign is unreachable"}),
        reason="OK",
    )
    assert result.ok is False
    assert "the call succeeded" not in result.headline
    assert "the body reports a failure" in result.headline


def test_a_real_success_still_says_so():
    result, _text = rendered_text(
        "simple_write_message",
        200,
        json.dumps({"result": "OK", "result_message": "Message displayed on sign"}),
        reason="OK",
    )
    assert result.ok is True
    assert "the call succeeded" in result.headline


def test_a_four_hundred_keeps_the_meaning_it_had():
    result, _text = rendered_text("get_message", 404, json.dumps({"detail": "nope"}))
    assert "no slot by that name" in result.headline


def test_the_rendered_html_takes_its_ink_from_the_theme():
    from relayclient.format import DARK, LIGHT

    result = render(catalogue.BY_ID["get_alert"], 200, "OK", "null")
    light = as_html(result, LIGHT)
    dark = as_html(result, DARK)
    assert LIGHT.ink in light and LIGHT.muted in light
    assert DARK.ink in dark and DARK.muted in dark
    # The point of the pair: neither theme's ink may leak into the other, which
    # is what produces grey on dark.
    assert DARK.ink not in light
    assert LIGHT.muted not in dark


def test_no_colour_the_themes_carry_is_written_down_instead_of_themed():
    # The test above renders a response whose only block is a Note, so it
    # reaches neither table renderer and neither accent. Put the light border
    # colours back into _table_html, or build the headline from a module
    # constant, and it would still pass. This one renders a table and a failure
    # too, so all six fields of the pair are exercised in both directions.
    from relayclient.format import DARK, LIGHT

    body = json.dumps(
        [
            {
                "key": "k",
                "label": "A",
                "message": "hello",
                "display_mode": "HOLD",
                "position": "MIDDLE",
                "order": 0,
                "source": None,
                "expires_at": None,
                "updated_at": "2026-08-31T07:00:00+00:00",
            }
        ]
    )
    table = render(catalogue.BY_ID["list_messages"], 200, "OK", body)
    failure = render(catalogue.BY_ID["list_messages"], 503, "Service Unavailable", "{}")

    for theme, other in ((LIGHT, DARK), (DARK, LIGHT)):
        html = as_html(table, theme) + as_html(failure, theme)
        for name in ("ink", "muted", "rule", "faint_rule", "ok", "bad"):
            assert getattr(theme, name) in html, name
            assert getattr(other, name) not in html, name


def test_an_enumeration_is_read_with_the_field_the_catalogue_names():
    # The simple display modes endpoint names its entries `display_mode`. A
    # payload in the other shape must not render a healthy table here while
    # enums.parse rejects it, which is what guessing the field produced.
    v2_shape = json.dumps([{"name": "HOLD", "description": "hold"}])
    _result, text = rendered_text("simple_display_modes", 200, v2_shape)
    assert "HOLD" not in text

    own_shape = json.dumps([{"display_mode": "HOLD", "description": "hold"}])
    _result, text = rendered_text("simple_display_modes", 200, own_shape)
    assert "HOLD" in text


def test_the_always_200_note_is_not_attached_to_a_status_that_means_it():
    body = json.dumps({"result": "ERROR", "result_message": "the sign is unreachable"})
    _result, text = rendered_text("simple_write_message", 503, body)
    assert "answers 200 whatever happens" not in text
    _result, text = rendered_text("simple_write_message", 200, body)
    assert "answers 200 whatever happens" in text
