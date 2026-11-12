"""Tests for alerts: takeover, timed release, and surviving a restart."""

import asyncio
import contextlib
from datetime import timedelta

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.services.alerts import (
    AlertAlreadyActive,
    AlertNotWaiting,
    AlertQueueFull,
    AlertService,
    AlertTooLong,
)
from readerboard.sign.controller import SignController
from readerboard.sign.state import AlertState, QueuedAlertState
from readerboard.transport.base import TransportError


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


class TestRefusingToReplace:
    """``if_active="fail"``: refuse rather than take the sign off another alert."""

    async def test_an_alert_replaces_one_already_up_by_default(self, alerts):
        await raise_alert(alerts, "FIRST")
        await raise_alert(alerts, "SECOND")

        assert alerts.active is not None
        assert alerts.active.message == "SECOND"

    async def test_refusing_leaves_the_alert_that_is_up_exactly_as_it_was(
        self, alerts, transport
    ):
        await raise_alert(alerts, "FIRST")
        transport.clear()

        with pytest.raises(AlertAlreadyActive):
            await raise_alert(alerts, "SECOND", if_active="fail")

        assert alerts.active is not None
        assert alerts.active.message == "FIRST"
        # Nothing was sent, so the sign is still drawing the first alert rather
        # than a blank priority file written on the way to the refusal.
        assert transport.packets == []

    async def test_it_is_raised_when_nothing_is_holding_the_sign(self, alerts):
        alert = await raise_alert(alerts, "ONLY", if_active="fail")
        assert alert.message == "ONLY"

    async def test_an_alert_past_its_deadline_does_not_count_as_holding_the_sign(
        self, alerts, clock
    ):
        # The sweep that releases an expired alert runs on a timer, so one that
        # ran out a moment ago is still recorded. Refusing for it would make the
        # answer depend on where in the second the call arrived.
        await raise_alert(alerts, "FIRST", ttl_seconds=30)
        clock.advance(31)

        await raise_alert(alerts, "SECOND", if_active="fail")

        assert alerts.active is not None
        assert alerts.active.message == "SECOND"

    async def test_an_alert_still_inside_its_deadline_does_count(self, alerts, clock):
        await raise_alert(alerts, "FIRST", ttl_seconds=30)
        clock.advance(29)

        with pytest.raises(AlertAlreadyActive, match="if_active"):
            await raise_alert(alerts, "SECOND", if_active="fail")


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
        controller = SignController(transport, inter_packet_delay=0, settle=False)
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
        # Released rather than left recorded, or every alert waiting behind it
        # would wait for good.
        assert restored.active is None

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


async def queue_alert(alerts, message, **kwargs):
    return await alerts.raise_alert(message, mode="HOLD", if_active="queue", **kwargs)


def priority_packet(text: bytes) -> bytes:
    return frames.packet(frames.write_text_file(c.FILE_PRIORITY, text))


class TestQueue:
    """``if_active="queue"``: wait for the alert holding the sign to end."""

    async def test_with_nothing_holding_the_sign_it_shows_at_once(self, alerts):
        alert = await queue_alert(alerts, "ONLY")

        assert isinstance(alert, AlertState)
        assert alerts.active is alert
        assert alerts.waiting == []

    async def test_behind_an_alert_it_waits_and_sends_nothing(self, alerts, transport):
        await raise_alert(alerts, "FIRST")
        transport.clear()

        waiting = await queue_alert(alerts, "SECOND")

        assert isinstance(waiting, QueuedAlertState)
        assert alerts.active.message == "FIRST"
        assert alerts.waiting == [waiting]
        assert alerts.position(waiting) == 1
        assert transport.packets == []

    async def test_releasing_lets_the_next_one_take_the_sign_at_the_sweep(
        self, alerts, transport
    ):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await alerts.release()
        transport.clear()

        assert await alerts.promote() is True

        assert alerts.active.message == "SECOND"
        assert alerts.waiting == []
        assert transport.last_packet == priority_packet(b"SECOND")

    async def test_an_expiry_lets_the_next_one_take_the_sign(self, alerts, clock):
        await raise_alert(alerts, "FIRST", ttl_seconds=30)
        await queue_alert(alerts, "SECOND")
        clock.advance(31)

        await alerts.sweep()
        assert await alerts.promote() is True

        assert alerts.active.message == "SECOND"

    async def test_its_ttl_counts_from_when_it_takes_the_sign(self, alerts, clock):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND", ttl_seconds=60)
        clock.advance(600)
        await alerts.release()

        await alerts.promote()

        assert alerts.active.started_at == clock.now
        assert alerts.active.expires_at == clock.now + timedelta(seconds=60)

    async def test_nothing_is_promoted_while_an_alert_holds_the_sign(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        assert await alerts.promote() is False
        assert alerts.active.message == "FIRST"

    async def test_they_take_the_sign_first_in_first_out(self, alerts):
        await raise_alert(alerts, "FIRST")
        for message in ("SECOND", "THIRD", "FOURTH"):
            await queue_alert(alerts, message)

        shown = []
        for _ in range(3):
            await alerts.release()
            await alerts.promote()
            shown.append(alerts.active.message)

        assert shown == ["SECOND", "THIRD", "FOURTH"]

    async def test_in_the_gap_before_a_promotion_a_queued_alert_joins_the_back(self, alerts):
        # Released and not yet swept: nothing shows, but one is waiting, and a
        # newcomer must not take the sign ahead of it.
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await alerts.release()

        late = await queue_alert(alerts, "THIRD")

        assert isinstance(late, QueuedAlertState)
        assert [w.message for w in alerts.waiting] == ["SECOND", "THIRD"]

    async def test_in_the_gap_before_a_promotion_fail_is_refused(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await alerts.release()

        with pytest.raises(AlertAlreadyActive, match="waiting") as refused:
            await raise_alert(alerts, "THIRD", if_active="fail")

        # Nothing is showing, so the remedy is the queue, not a release.
        assert "holding the sign" not in str(refused.value)
        assert "DELETE /alerts/queue" in str(refused.value)

    async def test_fail_behind_a_queue_is_told_that_a_release_promotes(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        with pytest.raises(AlertAlreadyActive) as refused:
            await raise_alert(alerts, "THIRD", if_active="fail")

        assert "1 more waiting" in str(refused.value)
        assert "clear_queue" in str(refused.value)

    async def test_fail_is_refused_while_one_is_waiting(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        with pytest.raises(AlertAlreadyActive):
            await raise_alert(alerts, "THIRD", if_active="fail")

    async def test_replace_leaves_the_waiting_alerts_alone(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        await raise_alert(alerts, "URGENT")

        assert alerts.active.message == "URGENT"
        assert [w.message for w in alerts.waiting] == ["SECOND"]

    async def test_a_key_already_waiting_updates_it_where_it_stands(self, alerts):
        await raise_alert(alerts, "FIRST")
        first = await queue_alert(alerts, "DOOR OPEN", key="door")
        await queue_alert(alerts, "POST", key="post")

        updated = await queue_alert(alerts, "DOOR STILL OPEN", key="door")

        assert [w.message for w in alerts.waiting] == ["DOOR STILL OPEN", "POST"]
        assert alerts.position(updated) == 1
        assert updated.queued_at == first.queued_at

    async def test_a_key_matching_the_alert_showing_joins_the_back(self, alerts):
        await raise_alert(alerts, "DOOR OPEN", key="door")

        waiting = await queue_alert(alerts, "DOOR STILL OPEN", key="door")

        assert alerts.active.message == "DOOR OPEN"
        assert alerts.waiting == [waiting]

    async def test_a_full_queue_refuses_a_new_alert(self, controller, store, state, clock):
        alerts = AlertService(controller, store, state, now=clock, queue_limit=2)
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await queue_alert(alerts, "THIRD", key="third")

        with pytest.raises(AlertQueueFull, match="alert_queue_limit"):
            await queue_alert(alerts, "FOURTH")
        # A key already waiting is an update, not a newcomer.
        await queue_alert(alerts, "THIRD AGAIN", key="third")

        assert [w.message for w in alerts.waiting] == ["SECOND", "THIRD AGAIN"]

    async def test_one_waiting_past_its_limit_is_dropped_unseen(self, alerts, clock, transport):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "STALE", max_wait_seconds=60)
        await queue_alert(alerts, "PATIENT")
        clock.advance(61)

        await alerts.sweep()

        assert [w.message for w in alerts.waiting] == ["PATIENT"]
        await alerts.release()
        await alerts.promote()
        assert alerts.active.message == "PATIENT"
        assert priority_packet(b"STALE") not in transport.packets

    async def test_a_queued_alert_too_long_is_refused_when_it_is_sent(self, alerts):
        await raise_alert(alerts, "FIRST")

        with pytest.raises(AlertTooLong):
            await queue_alert(alerts, "X" * 126)

        assert alerts.waiting == []

    async def test_one_that_can_no_longer_be_shown_is_dropped_and_the_next_follows(
        self, alerts, state, caplog
    ):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "BROKEN")
        await queue_alert(alerts, "FINE")
        # Something that fitted when it was queued and does not now, as a
        # markup token a later version removed would leave it.
        state.alert_queue[0].message = "X" * 126
        await alerts.release()

        with caplog.at_level("WARNING"):
            # In the same call, so that a release asked for by a caller is not
            # followed by a gap with nothing showing.
            assert await alerts.promote() is True
        assert "dropped the waiting alert" in caplog.text
        # The refusal's own full stop does not run into the sentence after it.
        assert ".. " not in caplog.text
        assert alerts.active.message == "FINE"
        assert alerts.waiting == []

    async def test_nothing_is_tried_while_the_link_is_down(self, alerts, transport):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await alerts.release()
        transport._is_open = False

        assert await alerts.promote() is False
        assert [w.message for w in alerts.waiting] == ["SECOND"]

    async def test_releasing_with_clear_queue_brings_the_rotation_back(self, alerts, transport):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        await alerts.release(clear_queue=True)

        assert alerts.waiting == []
        assert await alerts.promote() is False
        assert transport.last_packet == frames.packet(frames.clear_priority_file())

    async def test_a_waiting_alert_can_be_cancelled_by_its_key(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "DOOR OPEN", key="door")
        await queue_alert(alerts, "POST", key="post")

        await alerts.cancel_waiting("door")

        assert [w.key for w in alerts.waiting] == ["post"]
        with pytest.raises(AlertNotWaiting):
            await alerts.cancel_waiting("door")

    async def test_clearing_the_queue_leaves_the_alert_showing(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        assert await alerts.clear_waiting() == 1

        assert alerts.active.message == "FIRST"
        assert alerts.waiting == []

    async def test_the_queue_survives_a_restart(self, alerts, transport, store, clock):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND", key="second", ttl_seconds=30, max_wait_seconds=600)

        controller = SignController(transport, inter_packet_delay=0, settle=False)
        restored = AlertService(controller, store, store.load(), now=clock)

        [waiting] = restored.waiting
        assert (waiting.message, waiting.key, waiting.ttl_seconds) == ("SECOND", "second", 30)
        assert waiting.wait_until == clock.now + timedelta(seconds=600)

    async def test_a_restored_queue_longer_than_a_lowered_limit_is_tried_to_the_end(
        self, alerts, transport, store, clock
    ):
        # Restarted with alert_queue_limit lowered below what is waiting, every
        # alert still gets its turn: one that can be shown behind three that
        # cannot goes up in the same release rather than being left waiting.
        await raise_alert(alerts, "FIRST")
        for broken in ("ONE", "TWO", "THREE"):
            await queue_alert(alerts, broken)
        await queue_alert(alerts, "FINE")
        state = store.load()
        for waiting in state.alert_queue[:3]:
            waiting.message = "X" * 126

        controller = SignController(transport, inter_packet_delay=0, settle=False)
        restored = AlertService(controller, store, state, now=clock, queue_limit=1)
        await restored.dismiss()

        assert restored.active.message == "FINE"
        assert restored.waiting == []

    async def test_a_release_that_fails_keeps_the_queue_it_was_asked_to_clear(
        self, alerts, transport
    ):
        # The caller is told the sign is unreachable and the alert is still up,
        # so the waiting alerts must still be there too.
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        transport.fail_with = "cable unplugged"

        with pytest.raises(TransportError):
            await alerts.release(clear_queue=True)

        assert alerts.active.message == "FIRST"
        assert [w.message for w in alerts.waiting] == ["SECOND"]

    async def test_a_promotion_whose_write_fails_stays_at_the_head(self, alerts, transport):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await alerts.release()
        # Open when promotion looks, and failing when it writes.
        transport.fail_with = "cable unplugged"

        with pytest.raises(TransportError):
            await alerts.promote()

        assert alerts.active is None
        assert [w.message for w in alerts.waiting] == ["SECOND"]

    async def test_a_wait_limit_passing_mid_promotion_loses_nothing(
        self, alerts, controller, clock
    ):
        # The sweep runs as a task of its own, so it can arrive while a release
        # is writing the next alert up. It used to drop that alert from the
        # queue mid-write, and the promotion then took out the one behind it.
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "A", key="a", max_wait_seconds=10)
        await queue_alert(alerts, "B", key="b")
        gate, entered = asyncio.Event(), asyncio.Event()
        write = controller.write_priority

        async def held(*args, **kwargs):
            entered.set()
            await gate.wait()
            return await write(*args, **kwargs)

        controller.write_priority = held
        dismissing = asyncio.create_task(alerts.dismiss())
        await entered.wait()
        clock.advance(11)
        sweeping = asyncio.create_task(alerts.sweep())
        await asyncio.sleep(0)
        gate.set()
        await dismissing
        await sweeping

        # A took the sign before its limit passed, and B is still waiting.
        assert alerts.active.message == "A"
        assert [w.key for w in alerts.waiting] == ["b"]

    async def test_a_wait_limit_passing_before_the_locks_drops_it_unshown(
        self, alerts, registry, clock
    ):
        # Checked again once the locks are held, so an alert whose limit ran out
        # while it waited for them is dropped rather than shown, and the next
        # one goes up in its place.
        alerts.set_rendering(registry.rendering)
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "A", key="a", max_wait_seconds=10)
        await queue_alert(alerts, "B", key="b")
        await alerts.release()

        async with registry.rendering(None):
            promoting = asyncio.create_task(alerts.promote())
            await asyncio.sleep(0)
            clock.advance(11)
        assert await promoting is True

        assert alerts.active.message == "B"
        assert alerts.waiting == []

    async def test_a_promotion_stops_for_an_alert_that_took_the_sign_meanwhile(
        self, alerts, registry
    ):
        # An urgent alert sent while the promotion waited for the locks gets
        # them first. Writing the waiting one over it would lose it, and its
        # caller has already been answered 200.
        alerts.set_rendering(registry.rendering)
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "PARCEL")
        await alerts.release()

        async with registry.rendering(None):
            smoke = asyncio.create_task(raise_alert(alerts, "SMOKE"))
            await asyncio.sleep(0)
            promoting = asyncio.create_task(alerts.promote())
            await asyncio.sleep(0)
        await smoke

        assert await promoting is False
        assert alerts.active.message == "SMOKE"
        assert [w.message for w in alerts.waiting] == ["PARCEL"]

    async def test_two_queued_alerts_sent_at_once_to_a_free_sign_show_one_and_queue_one(
        self, alerts, registry
    ):
        # Both see a free sign while the registry is busy, so the second finds
        # it taken only when it goes to show, and has to go round again and
        # queue rather than fail.
        alerts.set_rendering(registry.rendering)

        async with registry.rendering(None):
            one = asyncio.create_task(queue_alert(alerts, "ONE"))
            two = asyncio.create_task(queue_alert(alerts, "TWO"))
            await asyncio.sleep(0)
        first, second = await asyncio.gather(one, two)

        assert isinstance(first, AlertState)
        assert isinstance(second, QueuedAlertState)
        assert alerts.active.message == "ONE"
        assert [w.message for w in alerts.waiting] == ["TWO"]

    async def test_the_sweep_releases_only_the_alert_it_found_expired(
        self, alerts, controller, clock
    ):
        # An alert queued after another's deadline, before the sweep, goes
        # straight up. The sweep found the old one expired, and used to release
        # whatever was holding the sign by the time it got the lock.
        await raise_alert(alerts, "OLD", ttl_seconds=5)
        clock.advance(6)
        gate, entered = asyncio.Event(), asyncio.Event()
        write = controller.write_priority

        async def held(*args, **kwargs):
            entered.set()
            await gate.wait()
            return await write(*args, **kwargs)

        controller.write_priority = held
        raising = asyncio.create_task(queue_alert(alerts, "NEW", ttl_seconds=60))
        await entered.wait()
        sweeping = asyncio.create_task(alerts.sweep())
        await asyncio.sleep(0)
        gate.set()
        shown = await raising

        assert await sweeping is False
        assert shown.message == "NEW"
        assert alerts.active.message == "NEW"

    async def test_dismissing_goes_past_a_head_that_has_to_be_dropped(self, alerts, state):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "BROKEN")
        await queue_alert(alerts, "FINE")
        state.alert_queue[0].message = "X" * 126

        await alerts.dismiss()

        assert alerts.active.message == "FINE"

    async def test_dismissing_puts_the_next_one_up_before_it_returns(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")
        await queue_alert(alerts, "THIRD")

        assert await alerts.dismiss() is True
        assert alerts.active.message == "SECOND"
        # So a second dismissal straight after reaches the one that followed.
        assert await alerts.dismiss() is True
        assert alerts.active.message == "THIRD"
        assert alerts.waiting == []

    async def test_dismissing_with_clear_queue_brings_the_rotation_back(self, alerts):
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        await alerts.dismiss(clear_queue=True)

        assert alerts.active is None
        assert alerts.waiting == []

    async def test_a_dismissal_whose_promotion_fails_still_released(
        self, alerts, transport, monkeypatch, caplog
    ):
        # The caller asked for a release and got one, so it is not told the
        # sign is unreachable; the next one stays at the head for the sweep.
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "SECOND")

        async def unreachable():
            raise TransportError("cable unplugged")

        monkeypatch.setattr(alerts, "promote", unreachable)
        with caplog.at_level("WARNING"):
            assert await alerts.dismiss() is True

        assert "could not show the next waiting one yet" in caplog.text
        assert alerts.active is None
        assert [w.message for w in alerts.waiting] == ["SECOND"]


class TestAnAlertOnTheSignIsSavedBeforeAnythingCanStopIt:
    """No await between putting an alert on the sign and saving it.

    The save used to wait for the registry's lock after the write. A task
    cancelled there, a request whose client went away or the sweep loop at
    shutdown, left the alert on the sign and the state file one alert behind,
    and a restart then put back the alert it had replaced, or showed a promoted
    one again with a fresh deadline.
    """

    async def _cancel_once_recorded(self, task, registry, state, message):
        # A refresh queued on the registry's lock is what kept the old save
        # waiting, so one is queued while the alert is being written.
        refreshing = asyncio.create_task(registry.refresh())
        while state.alert is None or state.alert.message != message:
            await asyncio.sleep(0)
        task.cancel()
        refreshing.cancel()
        for running in (task, refreshing):
            with contextlib.suppress(asyncio.CancelledError):
                await running

    async def test_a_raised_alert_is_in_the_file_when_its_request_is_cancelled(
        self, alerts, registry, state, store
    ):
        alerts.set_rendering(registry.rendering)
        await raise_alert(alerts, "OLD")

        raising = asyncio.create_task(raise_alert(alerts, "NEW"))
        await asyncio.sleep(0)
        await self._cancel_once_recorded(raising, registry, state, "NEW")

        assert store.load().alert.message == "NEW"

    async def test_a_promoted_alert_is_in_the_file_when_the_sweep_is_cancelled(
        self, alerts, registry, state, store
    ):
        alerts.set_rendering(registry.rendering)
        await raise_alert(alerts, "FIRST")
        await queue_alert(alerts, "DOOR OPEN")
        await alerts.release()

        promoting = asyncio.create_task(alerts.promote())
        await asyncio.sleep(0)
        await self._cancel_once_recorded(promoting, registry, state, "DOOR OPEN")

        saved = store.load()
        assert saved.alert.message == "DOOR OPEN"
        assert saved.alert_queue == []
