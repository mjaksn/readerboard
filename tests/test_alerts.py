"""Tests for alerts: takeover, timed release, and surviving a restart."""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.services.alerts import AlertService, AlertTooLong
from readerboard.sign.controller import SignController


async def raise_alert(alerts, message="<red>ALERT", **kwargs):
    return await alerts.raise_alert(message, mode="HOLD", **kwargs)


class TestTakeover:
    async def test_an_alert_writes_the_priority_file(self, alerts, transport):
        await raise_alert(alerts, "<red>ALERT")

        assert transport.last_packet == frames.packet(
            frames.write_text_file(c.FILE_PRIORITY, c.TEXT_COLOR_RED + b"ALERT")
        )

    async def test_the_alert_is_reported_as_active(self, alerts):
        await raise_alert(alerts)
        assert alerts.active is not None
        assert alerts.active.message == "<red>ALERT"

    async def test_an_alert_longer_than_the_priority_file_is_rejected(self, alerts):
        # The priority file is a fixed 125 bytes and the sign will not resize it,
        # so this has to fail rather than be silently truncated on the display.
        with pytest.raises(AlertTooLong, match="125"):
            await raise_alert(alerts, "X" * 126)

    async def test_an_alert_exactly_filling_the_priority_file_is_allowed(self, alerts):
        await raise_alert(alerts, "X" * 125)
        assert alerts.active is not None


class TestRelease:
    async def test_releasing_writes_an_empty_priority_file(self, alerts, transport):
        await raise_alert(alerts)
        await alerts.release()

        assert transport.last_packet == frames.packet(frames.clear_priority_file())
        assert alerts.active is None

    async def test_releasing_reports_whether_anything_was_holding_the_sign(self, alerts):
        assert await alerts.release() is False
        await raise_alert(alerts)
        assert await alerts.release() is True


class TestTimedRelease:
    async def test_an_alert_without_a_deadline_is_never_swept(self, alerts, clock):
        await raise_alert(alerts)
        clock.advance(86400)
        assert await alerts.sweep() is False
        assert alerts.active is not None

    async def test_an_alert_is_released_when_its_deadline_passes(self, alerts, clock):
        await raise_alert(alerts, ttl_seconds=30)

        clock.advance(29)
        assert await alerts.sweep() is False
        assert alerts.active is not None

        clock.advance(2)
        assert await alerts.sweep() is True
        assert alerts.active is None

    async def test_sweeping_with_no_alert_does_nothing(self, alerts):
        assert await alerts.sweep() is False


class TestRestart:
    def rebuild(self, transport, store, clock) -> AlertService:
        controller = SignController(transport, inter_packet_delay=0)
        return AlertService(controller, store, store.load(), now=clock)

    async def test_an_alert_still_within_its_deadline_comes_back(
        self, alerts, transport, store, clock
    ):
        await raise_alert(alerts, "<red>ALERT", ttl_seconds=300)

        clock.advance(10)
        restored = self.rebuild(transport, store, clock)
        await restored.restore()

        assert restored.active is not None
        assert restored.active.message == "<red>ALERT"
        assert transport.last_packet == frames.packet(
            frames.write_text_file(c.FILE_PRIORITY, c.TEXT_COLOR_RED + b"ALERT")
        )

    async def test_an_alert_that_expired_while_down_is_released(
        self, alerts, transport, store, clock
    ):
        # Without this the sign would come back still showing the alert, with
        # the rotation invisible behind it and nothing left to release it.
        await raise_alert(alerts, ttl_seconds=30)

        clock.advance(600)
        restored = self.rebuild(transport, store, clock)
        await restored.restore()

        assert restored.active is None
        assert transport.last_packet == frames.packet(frames.clear_priority_file())

    async def test_an_empty_alert_written_by_an_older_version_is_let_go(
        self, alerts, transport, store, clock, caplog
    ):
        # The HTTP surface refuses an empty alert now, but a state file written
        # before it did still holds one, and restoring it put a blank priority
        # message on the sign, which the sign displays rather than treating as a
        # release. The sign sat blank with the rotation suppressed behind it
        # while the service reported an alert nobody could read. Raised here
        # through the service, which is what the older version's HTTP layer
        # reached.
        await raise_alert(alerts, "")

        restored = self.rebuild(transport, store, clock)
        with caplog.at_level("WARNING"):
            await restored.restore()

        assert restored.active is None
        assert "no message" in caplog.text
        # And it stays gone: the state file is what the next restart reads.
        assert store.load().alert is None

    async def test_an_alert_that_outgrew_the_priority_file_is_let_go(
        self, alerts, transport, store, clock, caplog
    ):
        # A stored alert is re-rendered leniently so that a markup token this
        # version no longer knows cannot stop it coming back. That leniency can
        # make it longer: the unknown tag returns as its own literal text, which
        # is more bytes than the control code it used to render to. Enough of
        # those and the alert no longer fits the sign's fixed priority file.
        #
        # Letting that raise would be far worse than losing the alert. The
        # exception is a ValueError rather than a TransportError, so it escapes
        # the startup path that tolerates an unreachable sign, and the service
        # then refuses to start on every attempt until somebody edits the state
        # file by hand on the machine.
        await raise_alert(alerts, "<red>" + "A" * 100)

        # Rewrite the state file to name a token this version has removed,
        # which is exactly what an upgrade from the previous release looks like.
        state = store.load()
        assert state.alert is not None
        state.alert.message = "<dbl_height_on>" * 8 + "A" * 100
        store.save(state)

        restored = self.rebuild(transport, store, clock)
        with caplog.at_level("WARNING"):
            await restored.restore()  # must not raise

        assert restored.active is None
        assert "no longer fits" in caplog.text
        assert store.load().alert is None
        assert transport.last_packet == frames.packet(frames.clear_priority_file())

    async def test_reasserting_an_alert_that_no_longer_fits_does_not_raise(
        self, alerts, transport, store, clock, caplog
    ):
        # reassert runs on a timer and on every reconnect, so this would be a
        # crash every refresh rather than a one-off failure at startup.
        await raise_alert(alerts, "<red>" + "A" * 100)
        state = store.load()
        assert state.alert is not None
        state.alert.message = "<dbl_height_on>" * 8 + "A" * 100
        store.save(state)

        restored = self.rebuild(transport, store, clock)
        with caplog.at_level("WARNING"):
            assert await restored.reassert() is False

        assert "no longer fits" in caplog.text

    async def test_starting_with_no_alert_still_releases_the_priority_file(
        self, transport, store, clock
    ):
        # An unclean stop can leave the sign holding an alert that the state
        # file knows nothing about, and there is no way to ask the sign.
        restored = self.rebuild(transport, store, clock)
        await restored.restore()

        assert transport.last_packet == frames.packet(frames.clear_priority_file())


async def test_an_alert_does_not_disturb_the_run_sequence(alerts, registry, transport):
    await registry.upsert("temperature", "HI", mode="HOLD")
    transport.clear()

    await raise_alert(alerts)
    await alerts.release()

    # Taking over and giving back is entirely the priority file's business. The
    # sign resumes the rotation by itself, so nothing should have rewritten it.
    prefix = frames.packet(b"")[: -len(b"\x04")]
    assert not any(packet.startswith(prefix + b"E.") for packet in transport.packets)


class TestReassert:
    """The periodic refresh has to put an alert back too.

    The refresh exists because the sign can be power cycled behind a still
    connected adapter. It restores the slots, but an alert lives in the priority
    file, which the registry never touches.
    """

    async def test_it_rewrites_the_active_alert(self, alerts, transport):
        await raise_alert(alerts, "<red>ALERT")
        transport.clear()

        assert await alerts.reassert() is True
        assert transport.last_packet == frames.packet(
            frames.write_text_file(c.FILE_PRIORITY, c.TEXT_COLOR_RED + b"ALERT")
        )

    async def test_it_does_nothing_when_no_alert_is_holding_the_sign(self, alerts, transport):
        transport.clear()

        assert await alerts.reassert() is False
        assert transport.packets == []

    async def test_a_released_alert_is_not_put_back(self, alerts, transport):
        await raise_alert(alerts)
        await alerts.release()
        transport.clear()

        assert await alerts.reassert() is False
        assert transport.packets == []
