"""What gets written down about a call, and what deliberately does not."""

from __future__ import annotations

from datetime import datetime

from relayclient import catalogue
from relayclient.history import History
from relayclient.request import API_KEY_HEADER, build

BASE = "http://127.0.0.1:8000"
KEY = "a-real-looking-key"


def record_a_call(operation_id="put_message", **overrides):
    """Put one call through the history and hand the record back."""
    operation = catalogue.BY_ID[operation_id]
    prepared = build(
        operation,
        BASE,
        path_values={"key": "kitchen"},
        body_values={"message": "hello"},
        api_key=KEY,
    )
    history = History()
    fields = {
        "prepared": prepared,
        "summary": operation.summary,
        "started_at": datetime(2026, 8, 31, 7, 18, 21),
        "status": 200,
        "reason": "OK",
        "ok": True,
        "duration_ms": 42,
        "response_headers": {"content-type": "application/json"},
        "response_body": '{"key": "kitchen"}',
    }
    fields.update(overrides)
    return history, history.append(**fields)


def test_the_key_is_gone_from_the_record_itself_not_just_from_the_display():
    _history, record = record_a_call()
    assert record.request_headers[API_KEY_HEADER] == "<redacted>"
    assert KEY not in str(record.request_headers)


def test_the_key_cannot_reach_the_curl_command_because_it_is_not_in_the_record():
    _history, record = record_a_call()
    command = record.as_curl()
    assert KEY not in command
    assert "$READERBOARD_API_KEY" in command


def test_the_curl_command_is_the_call_that_was_made():
    _history, record = record_a_call()
    command = record.as_curl()
    assert "curl -X PUT 'http://127.0.0.1:8000/v2/messages/kitchen'" in command
    assert '"message": "hello"' in command


def test_a_call_that_never_completed_reads_as_failed_rather_than_as_status_zero():
    _history, record = record_a_call(status=0, reason="Connection refused", ok=False)
    assert record.outcome == "failed"


def test_records_are_numbered_and_come_back_newest_first():
    history = History()
    operation = catalogue.BY_ID["health"]
    prepared = build(operation, BASE)
    for _ in range(3):
        history.append(
            prepared=prepared,
            summary=operation.summary,
            started_at=datetime(2026, 8, 31, 7, 18, 21),
            status=200,
            reason="OK",
            ok=True,
            duration_ms=1,
            response_headers={},
            response_body="{}",
        )
    assert len(history) == 3
    assert [record.index for record in history.latest_first()] == [3, 2, 1]


def test_the_timestamp_is_shown_to_the_second():
    _history, record = record_a_call()
    assert record.stamp == "07:18:21"


def test_the_response_body_is_kept_exactly_as_it_arrived():
    _history, record = record_a_call(response_body='{"key":   "kitchen"}')
    assert record.response_body == '{"key":   "kitchen"}'
