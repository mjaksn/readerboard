"""Tests for the HTTP surface, including the exact payloads already in use."""

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from readerboard.api.app import create_app
from readerboard.config import Settings
from readerboard.transport.fake import FakeTransport

KEY = "test-key-not-a-real-one"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture
def sign() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_key=KEY,
        state_path=tmp_path / "state.json",
        serial_url="loop://",
        inter_packet_delay=0,
        slot_count=3,
        slot_capacity=256,
        clock_sync_enabled=False,
        refresh_interval_seconds=3600,
        registry_sweep_seconds=3600,
    )


@pytest.fixture
def client(settings, sign) -> Iterator[TestClient]:
    with TestClient(create_app(settings, transport=sign)) as client:
        yield client


class TestHealth:
    def test_it_needs_no_key(self, client):
        assert client.get("/health").status_code == 200

    def test_it_reports_the_link_and_the_pool(self, client):
        body = client.get("/health").json()

        assert body["status"] == "ok"
        assert body["link"]["connected"] is True
        assert body["slots_total"] == 3
        assert body["slots_used"] == 0
        assert body["sign_in_sync"] is True
        assert body["alert_active"] is False

    def test_it_never_reveals_the_key(self, client):
        assert KEY not in client.get("/health").text


class _AnyDescription:
    """Matches any description that tells a reader where the key comes from."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, str) and "READERBOARD_API_KEY" in other

    def __repr__(self) -> str:
        return "<a description mentioning READERBOARD_API_KEY>"


ANY_DESCRIPTION = _AnyDescription()


class TestAuth:
    def test_a_write_without_a_key_is_refused(self, client):
        response = client.put("/v2/messages/temperature", json={"message": "HI"})
        assert response.status_code == 401

    def test_a_write_with_the_wrong_key_is_refused(self, client):
        response = client.put(
            "/v2/messages/temperature",
            json={"message": "HI"},
            headers={"X-API-Key": "wrong"},
        )
        assert response.status_code == 401

    def test_a_write_with_the_key_is_allowed(self, client):
        response = client.put(
            "/v2/messages/temperature", json={"message": "HI"}, headers=HEADERS
        )
        assert response.status_code == 200

    def test_reads_do_not_need_a_key(self, client):
        assert client.get("/v2/messages").status_code == 200

    def test_the_simple_endpoints_also_need_it(self, client):
        # One of the three exceptions to their always-200 rule; the others are
        # a service with no API key configured (503) and a malformed body (422).
        response = client.post(
            "/Write/Message", json={"display_mode": "HOLD", "message": "HI"}
        )
        assert response.status_code == 401

    def test_the_refusal_says_which_header_is_wanted(self, client):
        # The scheme is declared with auto_error=False precisely so this wording
        # and the 503 below stay ours rather than becoming "Not authenticated".
        response = client.put("/v2/messages/temperature", json={"message": "HI"})
        assert response.json()["detail"] == "a valid X-API-Key header is required"
        assert response.headers["WWW-Authenticate"] == "X-API-Key"


class TestTheKeyIsDeclaredAsASecurityScheme:
    """What puts the Authorize button in the Swagger UI.

    The key was a plain header parameter once, which worked but told neither
    the documentation page nor a generated client that it was a credential.
    These pin the shape rather than the rendering, since the rendering is
    Swagger's business.
    """

    def test_the_scheme_is_declared(self, settings, sign):
        schema = create_app(settings, transport=sign).openapi()
        assert schema["components"]["securitySchemes"] == {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": ANY_DESCRIPTION,
            }
        }

    def test_every_write_requires_it(self, settings, sign):
        schema = create_app(settings, transport=sign).openapi()
        for path, method in [
            ("/v2/messages/{key}", "put"),
            ("/v2/messages/{key}", "delete"),
            ("/v2/messages", "delete"),
            ("/v2/alerts", "post"),
            ("/v2/alerts", "delete"),
            ("/v2/sign/sync-clock", "post"),
            ("/v2/sign/command", "post"),
            ("/Write/Message", "post"),
            ("/Write/ControlCommand", "post"),
        ]:
            assert schema["paths"][path][method]["security"] == [{"ApiKeyAuth": []}], (
                "%s %s should be marked as needing the key" % (method.upper(), path)
            )

    def test_the_open_endpoints_are_not_marked_as_needing_it(self, settings, sign):
        # Health is deliberately unauthenticated so a monitor can watch the sign
        # without holding a key that could write to it, and the reads are open
        # too. The document should say so rather than leave it to be guessed.
        schema = create_app(settings, transport=sign).openapi()
        for path, method in [
            ("/health", "get"),
            ("/v2/messages", "get"),
            ("/v2/alerts", "get"),
            ("/v2/enumerations/display-modes", "get"),
        ]:
            assert "security" not in schema["paths"][path][method]

    def test_every_component_key_is_one_the_specification_allows(self, settings, sign):
        # OpenAPI 3.1 section 4.8.7.1: "All the fixed fields declared above are
        # objects that MUST use keys that match the regular expression:
        # ^[a-zA-Z0-9\.\-_]+$". The scheme name became one of those keys, and a
        # readable "API key" with a space in it made the whole document invalid
        # for anything stricter than the Swagger UI. Nothing here catches that by
        # itself: the CI check only diffs the generated document against the
        # committed one, so both sides would be equally wrong.
        schema = create_app(settings, transport=sign).openapi()
        allowed = re.compile(r"^[a-zA-Z0-9._-]+$")
        for section, entries in schema.get("components", {}).items():
            for key in entries:
                assert allowed.match(key), "components.%s has the key %r" % (section, key)

    def test_the_header_is_no_longer_a_parameter_on_every_operation(self, settings, sign):
        # The old shape put an optional X-API-Key parameter on each protected
        # operation, which is what a reader had to fill in one endpoint at a
        # time. One scheme replaces all of them.
        schema = create_app(settings, transport=sign).openapi()
        for path, operations in schema["paths"].items():
            for method, operation in operations.items():
                names = [one["name"] for one in operation.get("parameters", [])]
                assert "X-API-Key" not in names, "%s %s" % (method.upper(), path)


class TestMessages:
    def test_registering_and_reading_back(self, client):
        client.put(
            "/v2/messages/temperature",
            json={"message": "<green>18.4<degree>", "display_mode": "HOLD"},
            headers=HEADERS,
        )

        body = client.get("/v2/messages/temperature").json()
        assert body["message"] == "<green>18.4<degree>"
        assert body["label"] == "A"

    def test_several_messages_share_the_sign(self, client):
        client.put("/v2/messages/one", json={"message": "ONE"}, headers=HEADERS)
        client.put("/v2/messages/two", json={"message": "TWO"}, headers=HEADERS)

        keys = [slot["key"] for slot in client.get("/v2/messages").json()]
        assert keys == ["one", "two"]

    def test_an_unknown_slot_is_404(self, client):
        assert client.get("/v2/messages/nobody").status_code == 404

    def test_deleting_an_unknown_slot_is_404(self, client):
        assert client.delete("/v2/messages/nobody", headers=HEADERS).status_code == 404

    def test_deleting_a_slot(self, client):
        client.put("/v2/messages/one", json={"message": "ONE"}, headers=HEADERS)
        assert client.delete("/v2/messages/one", headers=HEADERS).status_code == 204
        assert client.get("/v2/messages").json() == []

    def test_an_unknown_markup_token_is_400(self, client):
        response = client.put(
            "/v2/messages/one", json={"message": "<nosuchtag>"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "unknown markup token" in response.json()["detail"]

    def test_a_message_too_long_for_a_slot_is_400(self, client):
        response = client.put(
            "/v2/messages/one", json={"message": "X" * 300}, headers=HEADERS
        )
        assert response.status_code == 400

    def test_a_full_pool_is_409(self, client):
        for key in ("one", "two", "three"):
            client.put("/v2/messages/%s" % key, json={"message": key}, headers=HEADERS)

        response = client.put("/v2/messages/four", json={"message": "X"}, headers=HEADERS)
        assert response.status_code == 409
        assert "slots are in use" in response.json()["detail"]

    def test_an_unknown_display_mode_is_422(self, client):
        response = client.put(
            "/v2/messages/one",
            json={"message": "HI", "display_mode": "NOSUCHMODE"},
            headers=HEADERS,
        )
        assert response.status_code == 422

    def test_an_unusable_slot_name_is_422(self, client):
        response = client.put(
            "/v2/messages/not a valid key", json={"message": "HI"}, headers=HEADERS
        )
        assert response.status_code == 422


class TestAlerts:
    def test_raising_and_releasing(self, client):
        response = client.post("/v2/alerts", json={"message": "<red>ALERT"}, headers=HEADERS)
        assert response.status_code == 200
        assert client.get("/health").json()["alert_active"] is True

        assert client.delete("/v2/alerts", headers=HEADERS).status_code == 204
        assert client.get("/health").json()["alert_active"] is False

    def test_no_alert_reads_as_null(self, client):
        assert client.get("/v2/alerts").json() is None

    def test_an_alert_over_the_priority_file_size_is_400(self, client):
        response = client.post("/v2/alerts", json={"message": "X" * 200}, headers=HEADERS)
        assert response.status_code == 400
        assert "125" in response.json()["detail"]


class TestSignCommands:
    def test_syncing_the_clock(self, client):
        response = client.post("/v2/sign/sync-clock", headers=HEADERS)
        assert response.status_code == 200
        assert "synced_at" in response.json()

    def test_a_control_command(self, client):
        response = client.post(
            "/v2/sign/command",
            json={"command": "SET_TIME", "parameter": "0930"},
            headers=HEADERS,
        )
        assert response.status_code == 204

    def test_an_unknown_command_is_400(self, client):
        response = client.post(
            "/v2/sign/command", json={"command": "NOPE", "parameter": ""}, headers=HEADERS
        )
        assert response.status_code == 400

    def test_a_bad_parameter_is_400(self, client):
        response = client.post(
            "/v2/sign/command",
            json={"command": "SET_TIME", "parameter": "9999"},
            headers=HEADERS,
        )
        assert response.status_code == 400


class TestEnumerations:
    @pytest.mark.parametrize(
        "path",
        [
            "/v2/enumerations/markup-tokens",
            "/v2/enumerations/display-modes",
            "/v2/enumerations/text-positions",
            "/v2/enumerations/control-commands",
        ],
    )
    def test_each_lists_something_described(self, client, path):
        body = client.get(path).json()
        assert body
        assert all(entry["name"] and entry["description"] for entry in body)


class TestUnreachableSign:
    """A validated registry write is satisfiable even with the sign unplugged.

    Alerts, the clock and control commands are not: immediacy is their point,
    so those are the ones that get a 503.
    """

    def test_a_registry_write_is_accepted(self, client, sign):
        sign.fail_with = "cable unplugged"

        response = client.put(
            "/v2/messages/temperature", json={"message": "18.4"}, headers=HEADERS
        )

        assert response.status_code == 200

    def test_health_says_the_sign_is_behind(self, client, sign):
        sign.fail_with = "cable unplugged"
        client.put("/v2/messages/temperature", json={"message": "18.4"}, headers=HEADERS)

        assert client.get("/health").json()["sign_in_sync"] is False

    def test_an_alert_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"

        response = client.post("/v2/alerts", json={"message": "ALERT"}, headers=HEADERS)

        assert response.status_code == 503

    def test_a_clock_sync_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"
        assert client.post("/v2/sign/sync-clock", headers=HEADERS).status_code == 503

    def test_a_control_command_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"
        response = client.post(
            "/v2/sign/command",
            json={"command": "SET_TIME", "parameter": "0930"},
            headers=HEADERS,
        )
        assert response.status_code == 503


class TestTheSimpleEndpoints:
    """The payload shapes these endpoints accept, written out in full.

    They exist for callers that post a fixed body to a fixed path, so the
    bodies below are spelled out rather than built. A change that breaks one
    of them breaks somebody's configuration file, which is exactly what these
    tests are here to notice.
    """

    def test_the_home_assistant_rest_command_payload(self, client):
        # A temperature and the time, the common shape: a value from somewhere
        # else, then <time> for the sign to fill in on its own.
        response = client.post(
            "/Write/Message",
            json={
                "display_mode": "HOLD",
                "message": "<green>18.4<degree> <red><time>",
            },
            headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.json() == {
            "result": "OK",
            "result_message": "Message displayed on sign",
        }

    def test_the_cron_line_payload(self, client):
        # SET_TIME takes the time as HHMM on a 24 hour clock.
        response = client.post(
            "/Write/ControlCommand",
            json={"command": "SET_TIME", "parameter": "2359"},
            headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.json() == {
            "result": "OK",
            "result_message": "Control command sent to sign",
        }

    def test_the_simple_message_goes_to_a_slot_not_the_priority_file(self, client):
        client.post(
            "/Write/Message",
            json={"display_mode": "HOLD", "message": "HI"},
            headers=HEADERS,
        )

        slots = client.get("/v2/messages").json()
        assert [slot["key"] for slot in slots] == ["default"]
        # File "0" is the priority file, which by protocol suppresses every
        # other message. Writing here would make this a one-message service.
        assert slots[0]["label"] != "0"

    def test_a_simple_message_coexists_with_a_registered_one(self, client):
        client.post(
            "/Write/Message",
            json={"display_mode": "HOLD", "message": "TEMP"},
            headers=HEADERS,
        )
        client.put("/v2/messages/doorbell", json={"message": "DING"}, headers=HEADERS)

        keys = sorted(slot["key"] for slot in client.get("/v2/messages").json())
        assert keys == ["default", "doorbell"]

    def test_a_bad_display_mode_is_still_a_200_with_an_error_body(self, client):
        response = client.post(
            "/Write/Message",
            json={"display_mode": "NOSUCHMODE", "message": "HI"},
            headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["result"] == "ERROR"
        assert "not valid" in response.json()["result_message"]

    def test_an_unknown_command_is_still_a_200_with_an_error_body(self, client):
        response = client.post(
            "/Write/ControlCommand",
            json={"command": "NOPE", "parameter": ""},
            headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["result"] == "ERROR"
        assert "Unrecognized control command" in response.json()["result_message"]

    def test_an_unreachable_sign_is_still_a_200_with_an_error_body(self, client, sign):
        sign.fail_with = "cable unplugged"

        response = client.post(
            "/Write/ControlCommand",
            json={"command": "SET_TIME", "parameter": "0930"},
            headers=HEADERS,
        )

        assert response.status_code == 200
        assert response.json()["result"] == "ERROR"

    def test_an_unknown_markup_token_is_passed_through_rather_than_rejected(self, client):
        # Lenient rendering, so a message written against a newer token set
        # still displays rather than failing outright.
        response = client.post(
            "/Write/Message",
            json={"display_mode": "HOLD", "message": "<nosuchtag>HI"},
            headers=HEADERS,
        )

        assert response.json()["result"] == "OK"

    @pytest.mark.parametrize(
        "path,key",
        [
            ("/Enumerations/DisplayModes", "display_mode"),
            ("/Enumerations/ControlCommands", "control_command"),
            ("/Enumerations/MarkupTokens", "token_text"),
        ],
    )
    def test_the_old_enumeration_shapes_are_unchanged(self, client, path, key):
        body = client.get(path).json()
        assert body
        assert key in body[0]
        assert "description" in body[0]

    def test_the_old_display_modes_are_all_still_there(self, client):
        names = {entry["display_mode"] for entry in client.get("/Enumerations/DisplayModes").json()}
        assert {"HOLD", "FLASH", "ROTATE"} <= names

    def test_the_old_markup_tokens_are_all_still_there(self, client):
        names = {entry["token_text"] for entry in client.get("/Enumerations/MarkupTokens").json()}
        assert {
            "<red>",
            "<green>",
            "<amber>",
            "<dimred>",
            "<dimgreen>",
            "<brown>",
            "<orange>",
            "<yellow>",
            "<rainbow1>",
            "<rainbow2>",
            "<color_mix>",
            "<flash_on>",
            "<flash_off>",
            "<degree>",
            "<fixed_width>",
            "<time>",
            "<week_day>",
        } <= names


class TestNoApiKeyConfigured:
    def test_writes_are_refused_rather_than_left_open(self, tmp_path, sign):
        settings = Settings(
            api_key="",
            state_path=tmp_path / "state.json",
            inter_packet_delay=0,
            clock_sync_enabled=False,
        )
        with TestClient(create_app(settings, transport=sign)) as client:
            response = client.put("/v2/messages/one", json={"message": "HI"})
            assert response.status_code == 503
            assert "no API key is configured" in response.json()["detail"]


def test_the_openapi_schema_can_be_produced_without_a_sign(settings, sign):
    # No server, no port and no sign: FastAPI builds the whole description
    # offline, which is what lets CI check the committed copy for drift.
    schema = create_app(settings, transport=sign).openapi()

    assert schema["info"]["title"] == "readerboard"
    assert "/v2/messages/{key}" in schema["paths"]
    assert "/Write/Message" in schema["paths"]
