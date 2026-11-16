"""What the service leaves on the sign when it stops cleanly, and how it gets it back."""

import asyncio
import contextlib
import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from readerboard import icons
from readerboard.api.app import _refresh_and_reassert, create_app
from readerboard.config import Settings
from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import render
from readerboard.services.alerts import AlertService
from readerboard.services.registry import SlotRegistry
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
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


def picture_reply(label: bytes, rows: list[str]) -> bytes:
    """Build a sign's answer to a read of a picture file that holds ``rows``.

    The shape is recorded in docs/protocol-notes.md and built the same way in
    ``test_pictures.py``.
    """
    data = b"%02X%02X" % (len(rows), max(len(row) for row in rows))
    data += b"".join(row.encode("ascii") + c.CR for row in rows)
    body = c.STX + c.COMMAND_WRITE_DOTS + label + data + c.ETX
    return (
        c.NUL * 20
        + c.SOH
        + c.SIGN_TYPE_RESPONSE
        + c.SIGN_ADDRESS_BROADCAST
        + body
        + b"%04X" % sum(body)
        + c.EOT
    )


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

    def test_an_exit_message_made_unusable_after_the_settings_is_refused_at_the_start(
        self, settings
    ):
        # Settings check it as they are read, and nothing checks an assignment
        # afterwards. Rendered at the stop, a failure would skip closing the
        # link; rendered at the start, it stops the start instead.
        settings.exit_message = "<var:temp> DOWN"
        sign = a_sign()
        with pytest.raises(ValueError), TestClient(create_app(settings, transport=sign)):
            pass
        assert sign.packets == []


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
            deadline = time.monotonic() + 10
            while client.app.state.alerts.sign_may_not_match:
                assert time.monotonic() < deadline, "the reconnect never settled the doubt"
                time.sleep(0.05)


class TestTheFirstRefresh:
    """The release a start sends can be lost, and the first refresh sends another.

    The adapter can be up while the sign behind it is switched off or still
    running its diagnostics, so the start's release goes nowhere, and the sign
    keeps its memory, exit write and all. A refresh that then finds its pictures
    intact would leave it there.
    """

    @pytest.fixture
    async def started(self, controller, store, state, clock):
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)
        # What the lifespan does, in its order.
        alerts.distrust_the_priority_file()
        await registry.restore()
        await alerts.restore()
        return registry, alerts

    async def test_it_releases_once_even_when_the_sign_is_intact(
        self, started, transport, state
    ):
        registry, alerts = started
        await registry.upsert("door", "<icon:lock> LOCKED", mode="HOLD")
        label = state.pictures["lock"].label.encode("ascii")
        rows = list(icons.resolve("lock", None))

        transport.clear()
        transport.replies.append(picture_reply(label, rows))
        await _refresh_and_reassert(registry, alerts)
        assert transport.packets.count(RELEASE) == 1
        assert not alerts.sign_may_not_match

        # And only once: after that, an intact sign is left alone.
        transport.clear()
        transport.replies.append(picture_reply(label, rows))
        await _refresh_and_reassert(registry, alerts)
        assert RELEASE not in transport.packets

    async def test_it_writes_a_recorded_alert_again_once(self, started, transport, state):
        registry, alerts = started
        await registry.upsert("door", "<icon:lock> LOCKED", mode="HOLD")
        await alerts.raise_alert("FIRE", mode="HOLD")
        label = state.pictures["lock"].label.encode("ascii")

        transport.clear()
        transport.replies.append(picture_reply(label, list(icons.resolve("lock", None))))
        await _refresh_and_reassert(registry, alerts)

        assert [packet for packet in transport.packets if b"FIRE" in packet] != []
        assert not alerts.sign_may_not_match


class TestEveryRePush:
    async def test_a_service_with_no_icons_releases_on_every_refresh(
        self, controller, store, state, clock, transport
    ):
        # Every refresh of a service with no icons re-pushes, and the re-push
        # forgets what the controller believed, so the release is sent again
        # each time. That is what repairs a sign that missed the start's
        # release and the first refresh's, switched off behind the adapter for
        # both.
        registry = SlotRegistry(controller, Layout(3, 256), store, state, now=clock)
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)
        alerts.distrust_the_priority_file()
        await registry.restore()
        await alerts.restore()
        await _refresh_and_reassert(registry, alerts)

        for _ in range(2):
            transport.clear()
            await _refresh_and_reassert(registry, alerts)
            assert RELEASE in transport.packets


class TestAnOwedRestoreThatCannotFinish:
    async def test_the_priority_file_is_released_when_no_alert_is_recorded(
        self, alerts, transport
    ):
        # Every refresh is refused while the restore is owed, so nothing else
        # would release what the last stop left.
        alerts.distrust_the_priority_file()
        await alerts.release_an_unrecorded_takeover()
        assert transport.packets == [RELEASE]

    async def test_a_recorded_alert_is_left_for_the_restore(self, alerts, transport, state):
        await alerts.raise_alert("FIRE", mode="HOLD")
        alerts.distrust_the_priority_file()
        transport.clear()
        await alerts.release_an_unrecorded_takeover()
        assert transport.packets == []


class TestTheSettings:
    @pytest.mark.parametrize(
        "message",
        [
            "<var:temp> DOWN",
            "<icon:lock> OFFLINE",
            "<nonsense>",
            "X" * (c.PRIORITY_FILE_CAPACITY + 1),
        ],
    )
    def test_an_exit_message_the_sign_could_not_be_left_with_is_refused(self, tmp_path, message):
        with pytest.raises(ValidationError, match="exit_message"):
            Settings(state_path=tmp_path / "state.json", exit_message=message)

    def test_one_that_will_never_be_used_is_not_checked(self, tmp_path):
        settings = Settings(
            state_path=tmp_path / "state.json",
            blank_on_exit=False,
            exit_message="<icon:lock> OFFLINE",
        )
        assert settings.exit_message == "<icon:lock> OFFLINE"

    def test_the_defaults_blank_the_sign(self, tmp_path):
        settings = Settings(state_path=tmp_path / "state.json")
        assert settings.blank_on_exit is True
        assert settings.exit_message == ""


class TestTheController:
    async def test_a_link_opened_again_under_the_exit_write_starts_no_repair(self):
        # The repair re-pushes the pictures, and a picture write hands the sign
        # back, which would undo the write the stop leaves behind.
        class Reopening(FakeTransport):
            reopens = 0

            def write(self, data: bytes) -> None:
                super().write(data)
                self.reopens += 1

        sign = Reopening()
        controller = SignController(sign, inter_packet_delay=0)
        repaired: list[bool] = []

        async def hook() -> None:
            repaired.append(True)

        await controller.start()
        controller.on_reconnect(hook)
        await controller.stop(leave_showing=b"")
        await asyncio.sleep(0)

        assert sign.packets == [priority_write(b"")]
        assert controller._repair_task is None
        assert repaired == []

    async def test_a_sign_out_of_reach_is_said_and_the_link_still_closed(self, caplog):
        sign = FakeTransport()
        controller = SignController(sign, inter_packet_delay=0)
        await controller.start()
        sign.fail_with = "no route to host"

        with caplog.at_level(logging.WARNING, logger="readerboard.sign.controller"):
            await controller.stop(leave_showing=b"")

        assert "could not take the sign down" in caplog.text
        assert sign.is_open is False

    async def test_a_write_a_cancelled_caller_left_running_lands_before_the_next(self):
        # The stop cancels the sweep and the refresh, and a write either of
        # them had already handed to a thread carries on. The exit write after
        # it has to land after it, or a release left running from a refresh
        # would hand the sign back over the blank. A pool of threads would let
        # the later write overtake.
        class Slow(FakeTransport):
            def __init__(self) -> None:
                super().__init__()
                self.started = threading.Event()
                self.go = threading.Event()

            def write(self, data: bytes) -> None:
                if b"FIRST" in data:
                    self.started.set()
                    self.go.wait(5)
                super().write(data)

        sign = Slow()
        controller = SignController(sign, inter_packet_delay=0)
        await controller.start()

        first = asyncio.create_task(controller.write_priority(b"FIRST"))
        await asyncio.to_thread(sign.started.wait, 5)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        second = asyncio.create_task(controller.write_priority(b"SECOND"))
        await asyncio.sleep(0.2)
        sign.go.set()
        await second
        await controller.stop()

        order = [packet for packet in sign.packets if b"FIRST" in packet or b"SECOND" in packet]
        assert [b"FIRST" in packet for packet in order] == [True, False]
