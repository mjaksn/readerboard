"""Tests for the HTTP surface."""

import re
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from readerboard.api import errors
from readerboard.api.app import create_app
from readerboard.config import Settings
from readerboard.protocol import frames
from readerboard.services import commands
from readerboard.sign.controller import RESET_SETTLE_SECONDS, SOUND_SETTLE_SECONDS
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
        # Nothing here is a sign, so there is no deaf window to sit out and a
        # reset settle would cost every test in this file ten seconds.
        settle_delays_enabled=False,
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
        assert body["variables_total"] == 8
        assert body["variables_used"] == 0
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
        response = client.put("/messages/temperature", json={"message": "HI"})
        assert response.status_code == 401

    def test_a_write_with_the_wrong_key_is_refused(self, client):
        response = client.put(
            "/messages/temperature",
            json={"message": "HI"},
            headers={"X-API-Key": "wrong"},
        )
        assert response.status_code == 401

    def test_a_write_with_the_key_is_allowed(self, client):
        response = client.put(
            "/messages/temperature", json={"message": "HI"}, headers=HEADERS
        )
        assert response.status_code == 200

    def test_reads_do_not_need_a_key(self, client):
        assert client.get("/messages").status_code == 200

    def test_the_refusal_says_which_header_is_wanted(self, client):
        # The scheme is declared with auto_error=False precisely so this wording
        # and the 503 below stay ours rather than becoming "Not authenticated".
        response = client.put("/messages/temperature", json={"message": "HI"})
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
            ("/messages/{key}", "put"),
            ("/messages/{key}/active", "put"),
            ("/messages/{key}", "delete"),
            ("/messages", "delete"),
            ("/variables/{name}", "put"),
            ("/variables/{name}", "delete"),
            ("/alerts", "post"),
            ("/alerts", "delete"),
            ("/sign/sync-clock", "post"),
            ("/sign/command", "post"),
            ("/sign/reboot", "post"),
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
            ("/messages", "get"),
            ("/variables", "get"),
            ("/variables/{name}", "get"),
            ("/alerts", "get"),
            ("/enumerations/display-modes", "get"),
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
            "/messages/temperature",
            json={"message": "<green>18.4<degree>", "display_mode": "HOLD"},
            headers=HEADERS,
        )

        body = client.get("/messages/temperature").json()
        assert body["message"] == "<green>18.4<degree>"
        assert body["label"] == "A"

    def test_several_messages_share_the_sign(self, client):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        client.put("/messages/two", json={"message": "TWO"}, headers=HEADERS)

        keys = [slot["key"] for slot in client.get("/messages").json()]
        assert keys == ["one", "two"]

    def test_an_unknown_slot_is_404(self, client):
        assert client.get("/messages/nobody").status_code == 404

    def test_deleting_an_unknown_slot_is_404(self, client):
        assert client.delete("/messages/nobody", headers=HEADERS).status_code == 404

    def test_deleting_a_slot(self, client):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        assert client.delete("/messages/one", headers=HEADERS).status_code == 204
        assert client.get("/messages").json() == []

    def test_an_unknown_markup_token_is_400(self, client):
        response = client.put(
            "/messages/one", json={"message": "<nosuchtag>"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "unknown markup token" in response.json()["detail"]

    def test_an_empty_message_is_rejected(self, client):
        # It is not how a slot is given back, and accepted it would hold one
        # open around nothing: the sign cycles to a file with no text in it and
        # the pool is a slot smaller for it. DELETE is the way.
        response = client.put("/messages/one", json={"message": ""}, headers=HEADERS)
        assert response.status_code == 422
        assert client.get("/messages").json() == []

    def test_a_message_too_long_for_a_slot_is_400(self, client):
        response = client.put(
            "/messages/one", json={"message": "X" * 300}, headers=HEADERS
        )
        assert response.status_code == 400

    def test_a_full_pool_is_409(self, client):
        for key in ("one", "two", "three"):
            client.put("/messages/%s" % key, json={"message": key}, headers=HEADERS)

        response = client.put("/messages/four", json={"message": "X"}, headers=HEADERS)
        assert response.status_code == 409
        assert "slots are in use" in response.json()["detail"]

    def test_an_unknown_display_mode_is_422(self, client):
        response = client.put(
            "/messages/one",
            json={"message": "HI", "display_mode": "NOSUCHMODE"},
            headers=HEADERS,
        )
        assert response.status_code == 422

    def test_an_unusable_slot_name_is_422(self, client):
        response = client.put(
            "/messages/not a valid key", json={"message": "HI"}, headers=HEADERS
        )
        assert response.status_code == 422

    def test_a_new_message_is_showing_and_expires_by_being_deleted(self, client):
        body = client.put(
            "/messages/one", json={"message": "ONE"}, headers=HEADERS
        ).json()

        assert body["active"] is True
        assert body["delete_on_expiry"] is True

    def test_a_delete_on_expiry_that_is_not_a_boolean_is_422(self, client):
        response = client.put(
            "/messages/one",
            json={"message": "HI", "delete_on_expiry": "burn"},
            headers=HEADERS,
        )
        assert response.status_code == 422


class TestHidingAMessage:
    """Taking a message off the display without giving up its slot."""

    def test_hiding_it_keeps_it_registered(self, client):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)

        body = client.put(
            "/messages/one/active", json={"active": False}, headers=HEADERS
        ).json()

        assert body["active"] is False
        assert body["message"] == "ONE"
        # Still listed, still holding its file: hidden is not deleted.
        assert [slot["key"] for slot in client.get("/messages").json()] == ["one"]
        assert client.get("/health").json()["slots_used"] == 1

    def test_showing_it_again_needs_no_copy_of_the_message(self, client):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        client.put("/messages/one/active", json={"active": False}, headers=HEADERS)

        body = client.put(
            "/messages/one/active", json={"active": True}, headers=HEADERS
        ).json()

        assert body["active"] is True
        assert body["message"] == "ONE"

    def test_replacing_the_message_does_not_show_it_again(self, client):
        # The trap this endpoint exists to avoid. A source re-sending the same
        # content on a timer would otherwise switch a hidden message back on
        # every few minutes, and nothing would say why.
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        client.put("/messages/one/active", json={"active": False}, headers=HEADERS)

        body = client.put(
            "/messages/one", json={"message": "TWO"}, headers=HEADERS
        ).json()

        assert body["active"] is False
        assert body["message"] == "TWO"

    def test_hiding_an_unknown_slot_is_404(self, client):
        response = client.put(
            "/messages/nobody/active", json={"active": False}, headers=HEADERS
        )
        assert response.status_code == 404

    def test_it_needs_a_key(self, client):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        assert (
            client.put("/messages/one/active", json={"active": False}).status_code == 401
        )

    def test_a_body_that_is_not_a_boolean_is_422(self, client):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        response = client.put(
            "/messages/one/active", json={"active": "maybe"}, headers=HEADERS
        )
        assert response.status_code == 422


class TestAlerts:
    def test_raising_and_releasing(self, client):
        response = client.post("/alerts", json={"message": "<red>ALERT"}, headers=HEADERS)
        assert response.status_code == 200
        assert client.get("/health").json()["alert_active"] is True

        assert client.delete("/alerts", headers=HEADERS).status_code == 204
        assert client.get("/health").json()["alert_active"] is False

    def test_no_alert_reads_as_null(self, client):
        assert client.get("/alerts").json() is None

    def test_an_alert_over_the_priority_file_size_is_400(self, client):
        response = client.post("/alerts", json={"message": "X" * 200}, headers=HEADERS)
        assert response.status_code == 400
        assert "125" in response.json()["detail"]

    def test_an_empty_alert_is_rejected(self, client):
        # An empty message renders to no bytes, but the write still carries the
        # formatting bytes around it, which the sign reads as a blank priority
        # message and displays. Accepted, it left the sign blank with the
        # rotation suppressed behind it and an alert recorded as active, so
        # GET /alerts reported one that nothing was displaying.
        response = client.post("/alerts", json={"message": ""}, headers=HEADERS)
        assert response.status_code == 422
        assert client.get("/alerts").json() is None
        assert client.get("/health").json()["alert_active"] is False

    def test_an_unknown_markup_token_in_an_alert_is_400(self, client):
        # An alert renders twice: once here, to decide whether to accept it, and
        # again on the re-assert path, which renders leniently so that an alert
        # already accepted cannot fail to come back. Harmonise the two and this
        # acceptance stops validating anything: an unknown tag would reach the
        # display as literal text instead of earning a 400, and nothing else in
        # the suite would notice.
        response = client.post(
            "/alerts", json={"message": "<nosuchtag>FIRE"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "unknown markup token" in response.json()["detail"]


class TestExpiry:
    """A ttl is kept by the service's own sweep, on the service's own clock.

    This runs the service with the default sweep interval and real time, which
    every other test here replaces. The interval used to be fifteen seconds, and
    a ten second ttl was timed on a real sign lasting eighteen and twenty two:
    each expiry waited for the next sweep to notice it.
    """

    def test_a_message_goes_within_a_second_or_so_of_its_ttl(self, tmp_path, sign):
        settings = Settings(
            api_key=KEY,
            state_path=tmp_path / "state.json",
            serial_url="loop://",
            inter_packet_delay=0,
            settle_delays_enabled=False,
            clock_sync_enabled=False,
            refresh_interval_seconds=3600,
        )
        with TestClient(create_app(settings, transport=sign)) as client:
            client.put(
                "/messages/doorbell",
                json={"message": "DOOR", "ttl_seconds": 0.2},
                headers=HEADERS,
            )
            started = time.monotonic()
            while client.get("/messages").json() and time.monotonic() - started < 5:
                time.sleep(0.05)
            waited = time.monotonic() - started
            remaining = client.get("/messages").json()

        assert remaining == []
        # Generous for a loaded CI runner, and still well short of the fifteen
        # seconds the old interval could take.
        assert waited < 3, "the message outlived its 0.2s ttl by %.1fs" % waited


class TestSignCommands:
    def test_syncing_the_clock(self, client):
        response = client.post("/sign/sync-clock", headers=HEADERS)
        assert response.status_code == 200
        assert "synced_at" in response.json()

    def test_a_control_command(self, client):
        response = client.post(
            "/sign/command",
            json={"command": "SET_TIME", "parameter": "0930"},
            headers=HEADERS,
        )
        assert response.status_code == 204

    @pytest.mark.parametrize(
        ("parameter", "enabled"),
        [("ON", True), ("OFF", False), ("off", False)],
    )
    def test_muting_and_unmuting_the_speaker(self, client, sign, parameter, enabled):
        sign.packets.clear()

        response = client.post(
            "/sign/command", json={"command": "SPEAKER", "parameter": parameter}, headers=HEADERS
        )

        assert response.status_code == 204
        assert sign.packets == [frames.packet(frames.set_speaker(enabled))]

    def test_an_unknown_speaker_setting_is_400(self, client):
        response = client.post(
            "/sign/command", json={"command": "SPEAKER", "parameter": "MUTE"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "ON" in response.json()["detail"]

    @pytest.mark.parametrize(
        ("parameter", "expected"),
        [
            ("TONE", frames.sound_tone),
            ("BEEPS", frames.sound_beeps),
            ("beeps", frames.sound_beeps),
        ],
    )
    def test_sounding_the_speaker(self, client, sign, parameter, expected):
        sign.packets.clear()

        response = client.post(
            "/sign/command", json={"command": "SOUND", "parameter": parameter}, headers=HEADERS
        )

        assert response.status_code == 204
        assert sign.packets == [frames.packet(expected())]

    def test_an_unknown_sound_is_400(self, client):
        response = client.post(
            "/sign/command", json={"command": "SOUND", "parameter": "SIREN"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "TONE" in response.json()["detail"]

    def test_sounding_the_speaker_waits_but_not_as_long_as_a_reset(self):
        # A beep is not a restart and still leaves the sign deaf: the protocol
        # switches its serial port off for the length of the tone and asks for
        # three seconds before anything else is sent. So SOUND waits, and waits
        # for its own duration rather than borrowing the reset's ten seconds.
        assert commands.quiet_seconds_after("SOUND") == SOUND_SETTLE_SECONDS
        assert SOUND_SETTLE_SECONDS < RESET_SETTLE_SECONDS

    def test_a_soft_reset(self, client, sign):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        sign.packets.clear()

        response = client.post(
            "/sign/command", json={"command": "SOFT_RESET", "parameter": ""}, headers=HEADERS
        )

        assert response.status_code == 204
        assert sign.packets == [frames.packet(frames.soft_reset())]

    def test_a_soft_reset_erases_nothing(self, client, sign):
        # The whole point of it. The destructive reset is POST /sign/reboot.
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        sign.packets.clear()

        client.post(
            "/sign/command", json={"command": "SOFT_RESET", "parameter": ""}, headers=HEADERS
        )

        assert frames.packet(frames.clear_memory()) not in sign.packets
        assert client.get("/messages").json()[0]["key"] == "one"

    def test_only_the_two_deafening_commands_make_the_route_wait(self):
        # The wait is what stops a write landing while the sign cannot hear it.
        # Spelled out so that a command which deafens the sign has to declare
        # itself here rather than quietly not waiting.
        assert commands.quiet_seconds_after("SOFT_RESET") == RESET_SETTLE_SECONDS
        assert commands.quiet_seconds_after("  soft_reset  ") == RESET_SETTLE_SECONDS
        for name in ("SET_TIME", "SET_DAY_OF_WEEK", "SET_TIME_FORMAT", "SPEAKER"):
            assert commands.quiet_seconds_after(name) == 0.0

    def test_a_soft_reset_with_a_parameter_is_400(self, client):
        # Refused rather than ignored: the caller meant something by it.
        response = client.post(
            "/sign/command", json={"command": "SOFT_RESET", "parameter": "9"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "takes no parameter" in response.json()["detail"]

    def test_rebooting_the_sign(self, client, sign):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)

        response = client.post("/sign/reboot", headers=HEADERS)

        assert response.status_code == 204
        # The clear went to the sign, and the message that was registered
        # survives the reset in the service's record.
        assert frames.packet(frames.clear_memory()) in sign.packets
        assert client.get("/messages").json()[0]["key"] == "one"

    def test_an_unknown_command_is_400(self, client):
        response = client.post(
            "/sign/command", json={"command": "NOPE", "parameter": ""}, headers=HEADERS
        )
        assert response.status_code == 400

    def test_a_bad_parameter_is_400(self, client):
        response = client.post(
            "/sign/command",
            json={"command": "SET_TIME", "parameter": "9999"},
            headers=HEADERS,
        )
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "parameter",
        [
            # Four superscript twos. str.isdigit is true of these and int is
            # not, so the guard passed the parameter to a conversion that
            # raised, and the caller was told 500 in plain text.
            "²²²²",
            # Arabic-Indic zero nine three zero. Both were true of these, so
            # the sign was quietly set to 09:30 by a parameter that is not the
            # HHMM the message asks for.
            "٠٩٣٠",
        ],
    )
    def test_a_parameter_of_digits_the_sign_never_meant_is_400(self, client, parameter):
        response = client.post(
            "/sign/command",
            json={"command": "SET_TIME", "parameter": parameter},
            headers=HEADERS,
        )
        assert response.status_code == 400
        assert "four ASCII digits" in response.json()["detail"]

    def test_a_day_of_week_of_digits_the_sign_never_meant_is_400(self, client):
        response = client.post(
            "/sign/command",
            json={"command": "SET_DAY_OF_WEEK", "parameter": "²"},
            headers=HEADERS,
        )
        assert response.status_code == 400


class TestSignInformation:
    """The service's only read, and the only place a silent sign is visible."""

    def reply(self, data: bytes) -> bytes:
        """Frame a data field the way the sign frames its answers.

        The checksum is the sum of every byte from STX to ETX as four hex digits,
        and the EOT after it is what tells the service the answer is complete.
        """
        body = b"\x02" + b"E" + b'"' + data + b"\x03"
        return b"\x00" * 20 + b"\x01" + b"0" + b"00" + body + b"%04X" % sum(body) + b"\x04"

    def test_it_reports_what_the_sign_says(self, client, sign):
        sign.replies = [self.reply(b"1044-160B01931433M004000,0BB8")]

        body = client.get("/sign/information", headers=HEADERS).json()

        assert body["firmware_version"] == "1044-160"
        assert body["firmware_revision"] == "B"
        assert body["firmware_released"] == "01/93"
        assert body["clock"] == "14:33"
        assert body["time_format"] == "24 hour"
        assert body["speaker_enabled"] is True
        assert body["memory_total"] == 0x4000
        assert body["memory_free"] == 0x0BB8

    def test_it_asks_the_sign_the_right_question(self, client, sign):
        sign.packets.clear()  # startup has already configured the sign
        sign.replies = [self.reply(b"1044-160B01931433M004000,0BB8")]

        client.get("/sign/information", headers=HEADERS)

        assert sign.packets == [frames.packet(frames.read_general_information())]

    def test_a_muted_sign_is_reported_as_muted(self, client, sign):
        # The answer to "SOUND does nothing", and the reason this endpoint is
        # worth having rather than being a curiosity.
        sign.replies = [self.reply(b"1044-160B01931433MFF4000,0BB8")]

        body = client.get("/sign/information", headers=HEADERS).json()

        assert body["speaker_enabled"] is False

    def test_a_sign_that_says_nothing_is_a_503(self, client, sign):
        # Nothing scripted, so the fake answers every read with silence.
        response = client.get("/sign/information", headers=HEADERS)

        assert response.status_code == 503

    def test_a_reply_that_will_not_parse_is_a_503_rather_than_a_500(self, client, sign):
        """An unreadable answer is the sign's failure, not the caller's.

        The reply is the whole of what this endpoint has, so one that will not
        parse leaves it with nothing to report, exactly as silence does. Without
        ReplyError in the status table it reached no handler of ours and came
        back as a 500, which reads as a bug in the service.
        """
        sign.replies = [self.reply(b"not a general information reply")]

        response = client.get("/sign/information", headers=HEADERS)

        assert response.status_code == 503

    def test_it_needs_the_api_key(self, client):
        # A read changes nothing and is still gated, because it reports the
        # sign's firmware and how full its memory is.
        assert client.get("/sign/information").status_code == 401

    def test_it_changes_nothing_on_the_sign(self, client, sign):
        client.put("/messages/one", json={"message": "ONE"}, headers=HEADERS)
        sign.packets.clear()
        sign.replies = [self.reply(b"1044-160B01931433M004000,0BB8")]

        client.get("/sign/information", headers=HEADERS)

        # One packet, and it is the question. Nothing was written.
        assert sign.packets == [frames.packet(frames.read_general_information())]
        assert client.get("/messages").json()[0]["key"] == "one"


class TestThereIsNoVerticalPosition:
    """The four positions are gone, and the absence is pinned in three places.

    They all drew the same thing on a sign one line high, which is what the
    document says of any one-line sign and what this one confirmed. A name for a
    distinction nobody can see is a promise the sign does not keep.

    The byte itself still goes out on every write, because the protocol requires
    it. ``tests/test_frames.py`` pins that; this pins the vocabulary.
    """

    def test_the_enumeration_endpoint_is_gone(self, client):
        assert client.get("/enumerations/text-positions").status_code == 404

    def test_a_message_carrying_a_position_is_refused_rather_than_ignored(self, client):
        # The request models forbid unknown fields, so an old caller is told
        # rather than quietly having its choice dropped. That is the whole
        # reason this is a 422 and not a 200.
        response = client.put(
            "/messages/temperature",
            json={"message": "HI", "position": "TOP"},
            headers=HEADERS,
        )
        assert response.status_code == 422

    def test_an_alert_carrying_a_position_is_refused_too(self, client):
        response = client.post(
            "/alerts",
            json={"message": "HI", "position": "TOP"},
            headers=HEADERS,
        )
        assert response.status_code == 422

    def test_a_stored_slot_no_longer_reports_one(self, client):
        client.put("/messages/temperature", json={"message": "HI"}, headers=HEADERS)
        body = client.get("/messages/temperature", headers=HEADERS).json()
        assert "position" not in body


class TestEnumerations:
    @pytest.mark.parametrize(
        "path",
        [
            "/enumerations/markup-tokens",
            "/enumerations/value-tokens",
            "/enumerations/display-modes",
            "/enumerations/control-commands",
        ],
    )
    def test_each_lists_something_described(self, client, path):
        body = client.get(path).json()
        assert body
        assert all(entry["name"] and entry["description"] for entry in body)

    def test_every_display_mode_is_still_offered(self, client):
        # The vocabulary a caller writes into display_mode. Spelled out rather
        # than read off the token table, which would agree with itself however
        # much had quietly dropped out of it.
        names = {entry["name"] for entry in client.get("/enumerations/display-modes").json()}
        assert {"HOLD", "FLASH", "ROTATE"} <= names

    def test_every_markup_token_is_still_offered(self, client):
        # The same, for the tokens a caller writes inline in a message. Losing
        # one of these silently turns somebody's working message into a 400.
        names = {entry["name"] for entry in client.get("/enumerations/markup-tokens").json()}
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
            "<var:name>",
        } <= names

    def test_a_value_is_offered_every_message_token_but_the_day_of_week(self, client):
        # The sign draws <week_day> as a literal 9 from inside a variable.
        message = {entry["name"] for entry in client.get("/enumerations/markup-tokens").json()}
        value = {entry["name"] for entry in client.get("/enumerations/value-tokens").json()}
        assert message - value == {"<week_day>", "<var:name>"}
        assert value < message


class TestVariables:
    def put_variable(self, client, name="temp", **body):
        body.setdefault("value", "72")
        return client.put("/variables/%s" % name, json=body, headers=HEADERS)

    def test_creating_one_answers_with_it(self, client):
        response = self.put_variable(client, value="72", source="thermometer")

        assert response.status_code == 200
        body = response.json()
        assert body["name"] == "temp"
        assert body["label"] == "a"
        assert body["value"] == "72"
        assert body["stale"] is False
        assert body["called_by"] == []
        assert body["called_by_alert"] is False
        assert body["source"] == "thermometer"

    def test_it_goes_on_the_sign_as_a_string_write(self, client, sign):
        self.put_variable(client, value="72")
        assert sign.last_packet == frames.packet(b"Ga72")

    def test_a_write_needs_the_key(self, client):
        assert client.put("/variables/temp", json={"value": "72"}).status_code == 401

    def test_it_can_be_read_back_and_listed(self, client):
        self.put_variable(client, "wind", value="5")
        self.put_variable(client, "temp", value="72")

        assert client.get("/variables/temp").json()["value"] == "72"
        assert [one["name"] for one in client.get("/variables").json()] == ["temp", "wind"]

    def test_the_callers_are_reported(self, client):
        self.put_variable(client)
        client.put("/messages/weather", json={"message": "T=<var:temp>"}, headers=HEADERS)

        assert client.get("/variables/temp").json()["called_by"] == ["weather"]

    def test_a_message_calling_one_that_does_not_exist_is_400(self, client):
        response = client.put(
            "/messages/weather", json={"message": "T=<var:temp>"}, headers=HEADERS
        )
        assert response.status_code == 400
        assert "no variable named 'temp'" in response.json()["detail"]

    def test_deleting_one_a_message_calls_is_409_and_names_it(self, client):
        self.put_variable(client)
        client.put("/messages/weather", json={"message": "T=<var:temp>"}, headers=HEADERS)

        response = client.delete("/variables/temp", headers=HEADERS)

        assert response.status_code == 409
        assert "'weather'" in response.json()["detail"]

    def test_deleting_one_nothing_calls_is_204(self, client):
        self.put_variable(client)
        assert client.delete("/variables/temp", headers=HEADERS).status_code == 204
        assert client.get("/variables/temp").status_code == 404

    def test_an_unknown_one_is_404(self, client):
        assert client.get("/variables/nobody").status_code == 404

    @pytest.mark.parametrize("name", ["Temp", "a-b", "x" * 33])
    def test_a_name_no_message_could_call_is_refused(self, client, name):
        assert self.put_variable(client, name).status_code == 422

    def test_a_value_too_long_is_400(self, client):
        response = self.put_variable(client, value="X" * 33)
        assert response.status_code == 400
        assert "empties it" in response.json()["detail"]

    def test_the_day_of_week_in_a_value_is_400(self, client):
        assert self.put_variable(client, value="<week_day>").status_code == 400

    def test_an_unreachable_sign_is_503_and_leaves_nothing_behind(self, client, sign):
        sign.fail_with = "cable unplugged"
        assert self.put_variable(client).status_code == 503
        sign.fail_with = None
        assert client.get("/variables").json() == []

    def test_health_counts_them(self, client):
        self.put_variable(client)
        assert client.get("/health").json()["variables_used"] == 1

    def test_an_alert_can_call_one(self, client):
        self.put_variable(client)
        response = client.post("/alerts", json={"message": "<var:temp>"}, headers=HEADERS)
        assert response.status_code == 200
        body = client.get("/variables/temp").json()
        assert body["called_by"] == []
        assert body["called_by_alert"] is True

    def test_an_alert_calling_a_variable_that_does_not_exist_is_400(self, client):
        response = client.post("/alerts", json={"message": "<var:temp>"}, headers=HEADERS)
        assert response.status_code == 400
        assert "no variable named 'temp'" in response.json()["detail"]

    def test_one_the_alert_calls_cannot_be_deleted_until_it_is_released(self, client):
        self.put_variable(client)
        client.post("/alerts", json={"message": "<var:temp>"}, headers=HEADERS)

        response = client.delete("/variables/temp", headers=HEADERS)
        assert response.status_code == 409
        assert "the alert holding the sign" in response.json()["detail"]

        client.delete("/alerts", headers=HEADERS)
        assert client.delete("/variables/temp", headers=HEADERS).status_code == 204
        assert client.get("/variables").json() == []


class TestUnreachableSign:
    """Every write that needs the sign returns 503 when it cannot be reached.

    Messages, alerts, the clock and control commands all put something on the
    sign, so an unreachable sign fails each of them the same way rather than a
    message quietly buffering while the rest report the outage.
    """

    def test_a_registry_write_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"

        response = client.put(
            "/messages/temperature", json={"message": "18.4"}, headers=HEADERS
        )

        assert response.status_code == 503
        # The transport's reason reaches the response, so a caller learns why
        # from the 503 itself rather than from a log it cannot see.
        assert "cable unplugged" in response.json()["detail"]

    def test_a_rejected_write_leaves_no_slot_behind(self, client, sign):
        sign.fail_with = "cable unplugged"
        client.put("/messages/temperature", json={"message": "18.4"}, headers=HEADERS)

        assert client.get("/messages").json() == []

    def test_health_says_the_link_is_down(self, client, sign):
        sign.fail_with = "cable unplugged"
        client.put("/messages/temperature", json={"message": "18.4"}, headers=HEADERS)

        assert client.get("/health").json()["link"]["connected"] is False

    def test_an_alert_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"

        response = client.post("/alerts", json={"message": "ALERT"}, headers=HEADERS)

        assert response.status_code == 503

    def test_a_clock_sync_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"
        assert client.post("/sign/sync-clock", headers=HEADERS).status_code == 503

    def test_a_control_command_is_503(self, client, sign):
        sign.fail_with = "cable unplugged"
        response = client.post(
            "/sign/command",
            json={"command": "SET_TIME", "parameter": "0930"},
            headers=HEADERS,
        )
        assert response.status_code == 503

    def test_a_reboot_is_503(self, client, sign):
        # A sign that is not answering cannot be rebooted, so the recovery path
        # says so rather than pretending it reset something.
        sign.fail_with = "cable unplugged"
        assert client.post("/sign/reboot", headers=HEADERS).status_code == 503


class TestTheErrorTable:
    """The table that decides what each of the service's failures answers with.

    Every entry becomes an exception handler, so an exception the table does
    not name reaches no handler of ours at all. That is deliberate, and the
    second test is what pins the consequence: it must be a 500 rather than
    something that blames the caller.
    """

    def test_every_exception_in_the_table_is_registered_as_a_handler(self, settings, sign):
        # The registration is a loop over the table today. Written out by hand
        # again, this is what would notice the entry somebody forgot to add.
        app = create_app(settings)

        for kind, _code in errors.STATUS_FOR_ERROR:
            assert kind in app.exception_handlers, (
                "%s is in the table but no handler answers it" % kind.__name__
            )

    def test_an_exception_the_table_does_not_name_is_a_500(self, settings, sign, monkeypatch):
        # The honest answer for something this service never planned for. It
        # must not fall through to 400, which would blame the caller for it.
        app = create_app(settings, transport=sign)

        async def explode(*_args, **_kwargs):
            raise RuntimeError("something nobody named")

        with TestClient(app, raise_server_exceptions=False) as client:
            monkeypatch.setattr(app.state.registry, "upsert", explode)
            response = client.put("/messages/one", json={"message": "HI"}, headers=HEADERS)

        assert response.status_code == 500


class TestNoApiKeyConfigured:
    def test_writes_are_refused_rather_than_left_open(self, tmp_path, sign):
        settings = Settings(
            api_key="",
            state_path=tmp_path / "state.json",
            inter_packet_delay=0,
            settle_delays_enabled=False,
            clock_sync_enabled=False,
        )
        with TestClient(create_app(settings, transport=sign)) as client:
            response = client.put("/messages/one", json={"message": "HI"})
            assert response.status_code == 503
            assert "no API key is configured" in response.json()["detail"]


def test_the_openapi_schema_can_be_produced_without_a_sign(settings, sign):
    # No server, no port and no sign: FastAPI builds the whole description
    # offline, which is what lets CI check the committed copy for drift.
    schema = create_app(settings, transport=sign).openapi()

    assert schema["info"]["title"] == "readerboard"
    assert "/messages/{key}" in schema["paths"]
