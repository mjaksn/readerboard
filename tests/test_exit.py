"""What the service leaves on the sign when it stops cleanly, and how it gets it back."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from readerboard.api.app import create_app
from readerboard.config import Settings
from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import render
from readerboard.sign.controller import SignController
from readerboard.transport.fake import FakeTransport

KEY = "test-key-not-a-real-one"
HEADERS = {"X-API-Key": KEY}

RELEASE = frames.packet(frames.clear_priority_file())


def a_sign() -> FakeTransport:
    """Make a sign that answers the pool reading a start which reallocates asks for.

    Without the answer every first start here would wait out the read's three
    second deadline. A roomy 16 kB pool, as in ``test_api.py``.
    """
    body = (
        c.STX
        + c.COMMAND_WRITE_SPECIAL
        + c.SF_GENERAL_INFORMATION
        + b"1044-160B01931433M004000,0BB8"
        + c.ETX
    )
    sign = FakeTransport()
    sign.replies.append(
        c.NUL * 20
        + c.SOH
        + c.SIGN_TYPE_RESPONSE
        + c.SIGN_ADDRESS_BROADCAST
        + body
        + b"%04X" % sum(body)
        + c.EOT
    )
    return sign


def priority_write(body: bytes) -> bytes:
    """Build the packet that leaves ``body`` held on the priority file."""
    return frames.packet(frames.write_text_file(c.FILE_PRIORITY, body, mode=c.MODE_HOLD))


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        api_key=KEY,
        state_path=tmp_path / "state.json",
        serial_url="loop://",
        inter_packet_delay=0,
        settle_delays_enabled=False,
        slot_count=3,
        clock_sync_enabled=False,
        check_interval_seconds=3600,
        registry_sweep_seconds=3600,
    )


def run(settings: Settings, sign: FakeTransport) -> None:
    """Start the service, put a message up, and stop it cleanly."""
    with TestClient(create_app(settings, transport=sign)) as client:
        client.put("/slots/one", json={"message": "ONE"}, headers=HEADERS)


class TestTheLastWrite:
    def test_a_clean_stop_leaves_the_sign_blank_by_default(self, settings):
        sign = a_sign()
        run(settings, sign)

        assert sign.packets[-1] == priority_write(b"")
        assert sign.is_open is False

    def test_an_exit_message_is_left_instead(self, settings):
        settings.exit_message = "<red>OFFLINE"
        sign = a_sign()
        run(settings, sign)

        assert sign.packets[-1] == priority_write(render("<red>OFFLINE"))

    def test_turned_off_it_leaves_the_sign_alone(self, settings):
        settings.blank_on_exit = False
        settings.exit_message = "OFFLINE"
        sign = a_sign()
        run(settings, sign)

        assert priority_write(b"") not in sign.packets
        assert all(b"OFFLINE" not in packet for packet in sign.packets)

    def test_a_sign_out_of_reach_does_not_stop_the_service_stopping(self, settings):
        sign = a_sign()
        with TestClient(create_app(settings, transport=sign)):
            sign.open_fails_with = "no route to host"
            sign.close()

        assert sign.packets[-1] != priority_write(b"")

    def test_a_recorded_alert_is_left_recorded_and_comes_back(self, settings):
        sign = a_sign()
        with TestClient(create_app(settings, transport=sign)) as client:
            raised = client.post("/alerts", json={"message": "FIRE"}, headers=HEADERS)
            assert raised.status_code in (200, 201)
        assert sign.packets[-1] == priority_write(b"")

        again = FakeTransport()
        with TestClient(create_app(settings, transport=again)) as client:
            assert client.get("/alerts").json()["message"] == "FIRE"
        assert any(b"FIRE" in packet for packet in again.packets)


class TestTheNextStart:
    def test_it_releases_what_the_last_stop_left(self, settings):
        run(settings, a_sign())

        again = FakeTransport()
        with TestClient(create_app(settings, transport=again)) as client:
            assert client.get("/health").json()["status"] == "ok"
        assert RELEASE in again.packets

    def test_one_that_could_not_reach_the_sign_releases_it_on_the_reconnect(self, settings):
        # No icons, so no picture write hands the sign back on the reconnect.
        # Without the start distrusting the priority file, the refresh after it
        # would leave the sign dark behind the last stop's write for good.
        run(settings, a_sign())

        unreachable = FakeTransport(open_fails_with="no route to host")
        with TestClient(create_app(settings, transport=unreachable)) as client:
            assert client.app.state.alerts.sign_may_not_match
            unreachable.open_fails_with = None
            deadline = time.monotonic() + 10
            while RELEASE not in unreachable.packets:
                assert time.monotonic() < deadline, "the reconnect never released the sign"
                time.sleep(0.05)
            assert client.app.state.alerts.sign_may_not_match is False


class TestTheSettings:
    @pytest.mark.parametrize(
        "message",
        ["<var:temp> DOWN", "<icon:lock> OFFLINE", "<nonsense>", "X" * (c.PRIORITY_FILE_CAPACITY + 1)],
    )
    def test_an_exit_message_the_sign_could_not_be_left_with_is_refused(self, tmp_path, message):
        with pytest.raises(ValidationError, match="exit_message"):
            Settings(state_path=tmp_path / "state.json", exit_message=message)

    def test_the_defaults_blank_the_sign(self, tmp_path):
        settings = Settings(state_path=tmp_path / "state.json")
        assert settings.blank_on_exit is True
        assert settings.exit_message == ""


class TestTheController:
    def test_a_link_opened_again_under_the_last_write_starts_no_repair(self):
        # The repair re-pushes the pictures, and a picture write hands the sign
        # back, which would undo the write the stop leaves behind.
        async def scenario() -> None:
            sign = FakeTransport()
            controller = SignController(sign, inter_packet_delay=0)

            async def hook() -> None:
                raise AssertionError("a repair ran after the stop")

            controller.on_reconnect(hook)
            await controller.stop()
            sign.reopens = 1  # type: ignore[attr-defined]
            controller._notice_a_reopen()
            assert controller._repair_task is None

        asyncio.run(scenario())
