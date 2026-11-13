"""Tests for the registry: upsert, ordering, TTL, capacity, and restart."""

import asyncio
import json
from datetime import timedelta

import pytest

from readerboard.api.app import _after_reconnect, _finish_the_startup_restore
from readerboard.protocol import frames
from readerboard.services.alerts import AlertService
from readerboard.services.registry import (
    LayoutFull,
    MessageTooLong,
    SignNotRestored,
    SlotRegistry,
    UnknownSlot,
)
from readerboard.sign import pool
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
from readerboard.sign.state import AlertState, QueuedAlertState, ServiceState, StateStore
from readerboard.transport.base import TransportError
from readerboard.transport.fake import FakeTransport


async def add(registry, key, message="HI", **kwargs):
    return await registry.upsert(key, message, mode="HOLD", **kwargs)


# Framing up to and including STX, so a test can say "a packet whose payload
# starts with this command" without rebuilding the whole frame.
FRAME_PREFIX = frames.packet(b"")[: -len(b"\x04")]


def payloads_starting(transport: FakeTransport, command: bytes) -> list[bytes]:
    """Every packet the transport saw whose payload begins with ``command``."""
    return [
        packet for packet in transport.packets if packet.startswith(FRAME_PREFIX + command)
    ]


def run_sequences(transport: FakeTransport) -> list[bytes]:
    """Every run sequence packet the transport saw, in order."""
    return payloads_starting(transport, b"E.")


def first_index(transport: FakeTransport, command: bytes) -> int:
    """Where the first packet starting with ``command`` fell, for pinning an order."""
    for index, packet in enumerate(transport.packets):
        if packet.startswith(FRAME_PREFIX + command):
            return index
    raise AssertionError("no packet starting with %r" % command)


class TestUpsert:
    async def test_a_new_slot_gets_a_file_and_goes_on_the_sign(self, registry, transport):
        slot = await add(registry, "temperature", "<green>18.4<degree>")

        assert slot.label == "A"
        assert slot.key == "temperature"
        assert transport.write_count >= 1

    async def test_upserting_replaces_rather_than_duplicates(self, registry):
        await add(registry, "temperature", "ONE")
        await add(registry, "temperature", "TWO")

        assert len(registry.list_slots()) == 1
        assert registry.get("temperature").message == "TWO"

    async def test_upserting_keeps_the_same_file(self, registry):
        first = await add(registry, "temperature", "ONE")
        second = await add(registry, "temperature", "TWO")
        assert first.label == second.label

    async def test_a_second_source_gets_its_own_file(self, registry):
        temperature = await add(registry, "temperature")
        doorbell = await add(registry, "doorbell")

        assert temperature.label == "A"
        assert doorbell.label == "B"

    async def test_an_unknown_markup_token_is_rejected(self, registry):
        with pytest.raises(Exception, match="unknown markup token"):
            await add(registry, "temperature", "<nosuchtag>")

    async def test_a_message_too_long_for_its_slot_is_rejected(self, registry):
        with pytest.raises(MessageTooLong, match="renders to"):
            await add(registry, "temperature", "X" * 300)

    async def test_a_rejected_message_does_not_leak_a_slot(self, registry):
        with pytest.raises(MessageTooLong):
            await add(registry, "temperature", "X" * 300)
        assert registry.occupancy == (0, 3)


class TestOrdering:
    async def test_the_run_sequence_follows_the_order_field(self, registry, transport):
        await add(registry, "third", order=30)
        await add(registry, "first", order=10)
        await add(registry, "second", order=20)

        assert [slot.key for slot in registry.list_slots()] == ["first", "second", "third"]
        assert run_sequences(transport)[-1].endswith(b"BCA" + b"\x04")

    async def test_equal_orders_fall_back_to_the_key(self, registry):
        await add(registry, "zebra")
        await add(registry, "aardvark")
        assert [slot.key for slot in registry.list_slots()] == ["aardvark", "zebra"]

    async def test_reordering_a_slot_rewrites_the_run_sequence(self, registry, transport):
        # The order field is what decides the sequence, so moving a slot within
        # it has to reach the sign. It used to be left until the next refresh,
        # with the sign playing the old order and the API reporting the new one.
        await add(registry, "first", order=10)
        await add(registry, "second", order=20)
        transport.clear()

        await add(registry, "first", order=30)

        assert [slot.key for slot in registry.list_slots()] == ["second", "first"]
        assert run_sequences(transport)[-1].endswith(b"BA" + b"\x04")

    async def test_reordering_a_hidden_slot_changes_nothing_on_the_sign(
        self, registry, transport
    ):
        # It is not named either way, so the sequence it would produce is the
        # one already there and the controller declines to send it again.
        await add(registry, "shown")
        await add(registry, "hidden", order=20)
        await registry.set_active("hidden", False)
        transport.clear()

        await add(registry, "hidden", order=-5)

        assert run_sequences(transport) == []

    async def test_updating_content_does_not_rewrite_the_run_sequence(
        self, registry, transport
    ):
        await add(registry, "temperature", "ONE")
        before = len(run_sequences(transport))
        await add(registry, "temperature", "TWO")
        assert len(run_sequences(transport)) == before


class TestCapacity:
    async def test_a_full_pool_refuses_with_something_actionable(self, registry):
        await add(registry, "one")
        await add(registry, "two")
        await add(registry, "three")

        with pytest.raises(LayoutFull, match="all 3 message slots"):
            await add(registry, "four")

    async def test_occupancy_reports_both_numbers(self, registry):
        await add(registry, "one")
        assert registry.occupancy == (1, 3)


class TestRemoval:
    async def test_removing_takes_it_out_of_the_rotation(self, registry, transport):
        await add(registry, "one")
        await add(registry, "two")

        await registry.remove("one")

        assert [slot.key for slot in registry.list_slots()] == ["two"]
        assert run_sequences(transport)[-1].endswith(b"B" + b"\x04")

    async def test_removing_frees_the_file_for_reuse(self, registry):
        await add(registry, "one")
        await registry.remove("one")
        assert (await add(registry, "two")).label == "A"

    async def test_removing_something_that_is_not_there_is_a_clean_error(self, registry):
        with pytest.raises(UnknownSlot, match="no slot named"):
            await registry.remove("nobody")

    async def test_clearing_removes_everything(self, registry):
        await add(registry, "one")
        await add(registry, "two")

        assert await registry.clear() == 2
        assert registry.list_slots() == []


class TestActive:
    """Switching a message off keeps it registered and takes it off the display.

    The run sequence names the active slots and nothing else, so hiding one is a
    sequence write and nothing more: the file keeps its text, and switching it
    back on is another sequence write, with the controller declining to send
    bytes the file already holds. The exception is hiding the last one, where
    the file is emptied as well, because a sign handed a sequence naming nothing
    was measured freezing on the message it was drawing and holding it there.
    See docs/protocol-notes.md.
    """

    async def test_hiding_one_takes_it_out_of_the_run_sequence(self, registry, transport):
        await add(registry, "one")
        await add(registry, "two")

        await registry.set_active("one", False)

        assert run_sequences(transport)[-1].endswith(b"B" + b"\x04")

    async def test_it_stays_registered_on_its_own_file(self, registry, layout):
        await add(registry, "one", "HELLO")

        await registry.set_active("one", False)

        slot = registry.get("one")
        assert slot.active is False
        assert slot.message == "HELLO"
        assert layout.slots.label_for("one") == b"A"
        # It still holds its slot, so the pool is no emptier for hiding it.
        assert registry.occupancy == (1, 3)

    async def test_hiding_one_with_others_playing_costs_one_sequence_write(
        self, registry, transport
    ):
        # The measurement this is built on: a sequence write disturbs the
        # display far less than rewriting a TEXT file, which restarts the
        # message in it. So hiding is the sequence and nothing else, or it
        # pays the larger disturbance for nothing.
        await add(registry, "one", "HELLO")
        await add(registry, "two")
        transport.clear()

        await registry.set_active("one", False)

        assert len(run_sequences(transport)) == 1
        assert payloads_starting(transport, b"A") == []
        assert payloads_starting(transport, b"B") == []

    async def test_hiding_one_leaves_its_text_on_the_sign(self, registry, transport):
        # Nothing cycles to it, so the bytes sit there unseen. That is what keeps
        # showing it again down to one sequence write.
        await add(registry, "one", "HELLO")
        await add(registry, "two")
        transport.clear()

        await registry.set_active("one", False)

        assert frames.packet(frames.write_text_file(b"A", b"")) not in transport.packets

    async def test_hiding_the_last_one_leaves_the_sequence_naming_nothing(
        self, registry, transport
    ):
        await add(registry, "one")

        await registry.set_active("one", False)

        assert run_sequences(transport)[-1].endswith(b"E.SU" + b"\x04")

    async def test_showing_it_again_costs_one_sequence_write(self, registry, transport):
        # The whole point. Its file still holds those bytes, so the write is
        # suppressed and the sign is told only to start playing it again.
        await add(registry, "one", "HELLO")
        await add(registry, "two")
        await registry.set_active("one", False)
        transport.clear()

        await registry.set_active("one", True)

        assert payloads_starting(transport, b"A") == []
        assert len(run_sequences(transport)) == 1
        assert run_sequences(transport)[-1].endswith(b"AB" + b"\x04")

    async def test_hiding_the_last_one_empties_its_file(self, registry, transport):
        # With nothing left in the sequence the sign freezes on what it was
        # drawing, so here the blank is what clears the display.
        await add(registry, "one", "HELLO")
        transport.clear()

        await registry.set_active("one", False)

        assert frames.packet(frames.write_text_file(b"A", b"")) in transport.packets

    async def test_showing_the_last_one_again_writes_the_message_back(
        self, registry, transport
    ):
        # Coming back from an empty rotation costs both writes, since the file
        # was emptied to break the freeze.
        await add(registry, "one", "HELLO")
        await registry.set_active("one", False)
        transport.clear()

        await registry.set_active("one", True)

        assert payloads_starting(transport, b"A")
        assert run_sequences(transport)[-1].endswith(b"A" + b"\x04")

    async def test_setting_it_to_what_it_already_is_writes_nothing(self, registry, transport):
        await add(registry, "one")
        transport.clear()

        await registry.set_active("one", True)

        assert transport.packets == []

    async def test_an_unknown_slot_is_a_clean_error(self, registry):
        with pytest.raises(UnknownSlot, match="no slot named"):
            await registry.set_active("nobody", True)

    async def test_replacing_the_message_leaves_it_hidden(self, registry):
        # A source that re-sends the same content every few minutes must not
        # switch a message back on that somebody deliberately hid.
        await add(registry, "one", "ONE")
        await registry.set_active("one", False)

        await add(registry, "one", "TWO")

        assert registry.get("one").active is False
        assert registry.get("one").message == "TWO"

    async def test_a_hidden_slots_new_message_still_reaches_its_file(
        self, registry, transport
    ):
        # Written into a file nothing cycles to, so it shows nothing now and
        # switching it back on stays one sequence write.
        await add(registry, "one", "ONE")
        await add(registry, "two")
        await registry.set_active("one", False)
        transport.clear()

        await add(registry, "one", "TWO")

        assert payloads_starting(transport, b"A")
        assert registry.get("one").message == "TWO"
        transport.clear()

        await registry.set_active("one", True)

        assert payloads_starting(transport, b"A") == []

    async def test_a_message_can_be_shown_by_the_same_call_that_writes_it(
        self, registry, transport
    ):
        # The one-call case this exists for: something that reports an event
        # writes the message and puts it up in the same breath, rather than
        # writing it and then making a second call to show it.
        await add(registry, "alarm", "ARMED")
        await add(registry, "other")
        await registry.set_active("alarm", False)
        transport.clear()

        slot = await add(registry, "alarm", "DISARMED", active=True)

        assert slot.active is True
        # The text before the sequence, so the sign is never sent to a file
        # that is still being written.
        assert first_index(transport, b"A") < first_index(transport, b"E.")
        assert run_sequences(transport)[-1].endswith(b"AB" + b"\x04")

    async def test_a_message_can_be_hidden_by_the_same_call_that_writes_it(
        self, registry, transport
    ):
        await add(registry, "alarm", "ARMED")
        await add(registry, "other")
        transport.clear()

        slot = await add(registry, "alarm", "DISARMED", active=False)

        assert slot.active is False
        assert slot.message == "DISARMED"
        # The sequence first this time. The other way round the new text would
        # land in a file the sign was still cycling to, restarting the message
        # for the moment before it went.
        assert first_index(transport, b"E.") < first_index(transport, b"A")

    async def test_saying_nothing_about_it_still_leaves_it_alone(self, registry):
        # The guard that made this a separate endpoint in the first place. It
        # survives because the field is absent by default, not because the
        # field is absent from the endpoint.
        await add(registry, "one", "ONE")
        await registry.set_active("one", False)

        await add(registry, "one", "TWO")
        assert registry.get("one").active is False

        await registry.set_active("one", True)
        await add(registry, "one", "THREE")
        assert registry.get("one").active is True

    async def test_a_new_slot_can_be_registered_already_hidden(self, registry, transport):
        await add(registry, "kept")
        transport.clear()

        slot = await add(registry, "later", "NOT YET", active=False)

        assert slot.active is False
        # It holds a file, and the rotation is exactly what it was, so there
        # was nothing to tell the sign about it.
        assert registry.occupancy == (2, 3)
        assert run_sequences(transport) == []

        transport.clear()
        await registry.set_active("later", True)
        assert run_sequences(transport)[-1].endswith(b"AB" + b"\x04")

    async def test_a_notification_shows_for_its_ttl_and_waits_for_the_next_one(
        self, registry, clock
    ):
        # End to end, the way an alarm panel would drive it: one call per event,
        # carrying the text, the deadline and the decision to show it.
        await add(
            registry,
            "alarm",
            "ALARM NOW ARMED",
            ttl_seconds=60,
            delete_on_expiry=False,
            active=True,
        )
        clock.advance(61)
        assert await registry.sweep() == ["alarm"]
        assert registry.get("alarm").active is False

        await add(
            registry,
            "alarm",
            "ALARM NOW DISARMED",
            ttl_seconds=60,
            delete_on_expiry=False,
            active=True,
        )

        slot = registry.get("alarm")
        assert slot.active is True
        assert slot.message == "ALARM NOW DISARMED"
        # A fresh deadline, so it goes off the display a minute from now rather
        # than at once on the deadline the last event left behind.
        assert slot.expires_at is not None
        assert await registry.sweep() == []

    async def test_a_hidden_slot_comes_back_hidden_after_a_restart(
        self, registry, store, transport, clock
    ):
        await add(registry, "one")
        await add(registry, "two")
        await registry.set_active("one", False)

        controller = SignController(transport, inter_packet_delay=0, settle=False)
        restored = SlotRegistry(controller, Layout(3, 256), store, store.load(), now=clock)
        await restored.restore()

        assert restored.get("one").active is False
        assert restored.get("two").active is True

    async def test_a_restart_names_only_the_ones_showing(
        self, registry, store, transport, clock
    ):
        await add(registry, "one")
        await add(registry, "two")
        await registry.set_active("one", False)
        transport.clear()

        controller = SignController(transport, inter_packet_delay=0, settle=False)
        restored = SlotRegistry(controller, Layout(3, 256), store, store.load(), now=clock)
        await restored.restore()

        assert run_sequences(transport)[-1].endswith(b"B" + b"\x04")


class TestExpiry:
    async def test_a_slot_without_a_ttl_never_expires(self, registry, clock):
        await add(registry, "temperature")
        clock.advance(86400)
        assert await registry.sweep() == []

    async def test_a_slot_expires_once_its_ttl_passes(self, registry, clock):
        await add(registry, "doorbell", ttl_seconds=60)

        clock.advance(59)
        assert await registry.sweep() == []

        clock.advance(2)
        assert await registry.sweep() == ["doorbell"]
        assert registry.list_slots() == []

    async def test_expiry_leaves_the_other_slots_alone(self, registry, clock):
        await add(registry, "temperature")
        await add(registry, "doorbell", ttl_seconds=60)

        clock.advance(61)
        await registry.sweep()

        assert [slot.key for slot in registry.list_slots()] == ["temperature"]

    async def test_expiry_frees_the_file(self, registry, clock):
        await add(registry, "doorbell", ttl_seconds=60)
        clock.advance(61)
        await registry.sweep()
        assert registry.occupancy == (0, 3)

    async def test_a_deadline_can_hide_a_slot_instead_of_dropping_it(
        self, registry, clock, layout
    ):
        await add(registry, "bins", "BINS", ttl_seconds=60, delete_on_expiry=False)

        clock.advance(61)
        assert await registry.sweep() == ["bins"]

        slot = registry.get("bins")
        assert slot.active is False
        assert slot.message == "BINS"
        # It kept its slot, so the pool is no emptier for the deadline passing.
        assert layout.slots.label_for("bins") == b"A"
        assert registry.occupancy == (1, 3)

    async def test_a_hidden_slot_does_not_expire_again(self, registry, clock):
        # The deadline has already done what it was for. Left in place, the
        # slot would be hidden again the moment it was shown.
        await add(registry, "bins", ttl_seconds=60, delete_on_expiry=False)
        clock.advance(61)
        await registry.sweep()

        assert registry.get("bins").expires_at is None
        await registry.set_active("bins", True)
        clock.advance(86400)
        assert await registry.sweep() == []
        assert registry.get("bins").active is True

    async def test_a_hidden_slot_leaves_the_run_sequence(self, registry, clock, transport):
        await add(registry, "kept")
        await add(registry, "bins", ttl_seconds=60, delete_on_expiry=False)
        clock.advance(61)

        await registry.sweep()

        assert run_sequences(transport)[-1].endswith(b"A" + b"\x04")

    async def test_the_two_outcomes_can_expire_in_the_same_sweep(
        self, registry, clock, layout
    ):
        await add(registry, "gone", ttl_seconds=60)
        await add(registry, "bins", ttl_seconds=60, delete_on_expiry=False)

        clock.advance(61)
        assert sorted(await registry.sweep()) == ["bins", "gone"]

        assert layout.slots.label_for("gone") is None
        assert registry.get("bins").active is False


class TestRestart:
    def rebuild(self, store, transport, clock, slot_count=3, slot_capacity=256):
        """Build a second registry over the same state file, as a restart would."""
        controller = SignController(transport, inter_packet_delay=0, settle=False)
        layout = Layout(slot_count, slot_capacity)
        state = store.load()
        return SlotRegistry(controller, layout, store, state, now=clock), layout

    async def test_slots_come_back(self, registry, store, transport, clock):
        await add(registry, "temperature", "<green>18.4<degree>")
        await add(registry, "doorbell", "DING")

        restored, _ = self.rebuild(store, transport, clock)
        await restored.restore()

        assert [slot.key for slot in restored.list_slots()] == ["doorbell", "temperature"]
        assert restored.get("temperature").message == "<green>18.4<degree>"

    async def test_files_are_reattached_to_the_same_slots(self, registry, store, transport, clock):
        await add(registry, "one")
        await add(registry, "two")

        restored, layout = self.rebuild(store, transport, clock)
        await restored.restore()

        assert layout.slots.label_for("one") == b"A"
        assert layout.slots.label_for("two") == b"B"
        # A new slot must not be handed a file that is already spoken for.
        assert (await add(restored, "three")).label == "C"

    async def test_an_unchanged_pool_is_not_reallocated(self, registry, store, transport, clock):
        await add(registry, "one")
        state = store.load()
        assert state.layout is not None

        transport.clear()
        restored, _ = self.rebuild(store, transport, clock)
        await restored.restore()

        assert payloads_starting(transport, b"E$") == []

    async def test_a_changed_pool_is_reallocated_and_the_sign_starts_empty(
        self, registry, store, transport, clock
    ):
        await add(registry, "one")

        transport.clear()
        restored, _ = self.rebuild(store, transport, clock, slot_count=5)
        await restored.restore()

        assert payloads_starting(transport, b"E$")
        # Reallocating erases the sign, so the slots cannot be claimed to survive.
        assert restored.list_slots() == []

    async def test_an_empty_slot_written_by_an_older_version_is_dropped(
        self, registry, store, transport, clock, caplog
    ):
        # The HTTP surface refuses an empty message now, but a state file
        # written before it did still holds one, and it would otherwise be
        # rewritten and left in the run sequence on every start. Registered
        # here through the service, which is what the older version's HTTP
        # layer reached.
        await add(registry, "blank", "")
        await add(registry, "doorbell", "DING")

        restored, layout = self.rebuild(store, transport, clock)
        await restored.restore()

        assert [slot.key for slot in restored.list_slots()] == ["doorbell"]
        assert "no message" in caplog.text
        # And it stays gone, with its file handed back to the pool.
        assert store.load().slots.keys() == {"doorbell"}
        assert layout.slots.label_for("blank") is None

    async def test_a_slot_outside_a_shrunken_pool_is_dropped(
        self, registry, store, transport, clock, caplog
    ):
        await add(registry, "one")
        await add(registry, "two")
        await add(registry, "three")

        # Shrinking the pool reallocates, which clears the sign outright, so
        # start from a state whose recorded layout already matches the new size.
        state = store.load()
        assert state.layout is not None
        state.layout.slot_count = 2
        state.layout.labels = ["A", "B"]
        store.save(state)

        restored, _ = self.rebuild(store, transport, clock, slot_count=2)
        await restored.restore()

        assert [slot.key for slot in restored.list_slots()] == ["one", "two"]
        assert "outside the current pool" in caplog.text


async def test_the_state_file_is_written_on_every_change(registry, store):
    await add(registry, "temperature")
    assert store.path.exists()
    assert "temperature" in store.path.read_text(encoding="utf-8")


class TestAHalfAppliedWrite:
    """A write that lands and then fails partway leaves the sign ahead of us.

    These paths send twice: the run sequence and then the file, or the other way
    round. If the first lands and the second raises, rolling the record back does
    not roll the sign back, so the two disagree. The registry marks itself dirty
    so that /health says so and the next refresh repairs it, rather than
    reporting a sign that is in sync when it is not.
    """

    class FailsAfter(FakeTransport):
        """A transport that takes ``budget`` more writes and then stops for good.

        Set ``budget`` immediately before the call under test, so that setting
        the registry up does not spend it.
        """

        budget = 1_000_000

        def write(self, data: bytes) -> None:
            if self.budget <= 0:
                self.fail_with = "cable unplugged"
            self.budget -= 1
            super().write(data)

    async def test_the_registry_says_it_is_out_of_step(self, layout, store, state, clock):
        transport = self.FailsAfter()
        controller = SignController(transport, inter_packet_delay=0, settle=False)
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "one", "ONE")
        await add(registry, "two", "TWO")
        assert registry.in_sync

        # Hiding through upsert writes the run sequence first and the file
        # second, so with one write left the sequence lands and the file does
        # not. The sign is then playing a rotation without "one" in it while
        # the restored record still says it is showing.
        transport.budget = 1
        with pytest.raises(TransportError):
            await add(registry, "one", "GONE", active=False)

        assert registry.get("one").active is True
        assert registry.get("one").message == "ONE"
        assert not registry.in_sync


class TestWritingWhileTheSignIsUnreachable:
    """A write that cannot reach the sign fails, and changes nothing.

    The registry used to accept such a write and converge later, so a caller
    learned the sign was unreachable only by reading /health. It now lets the
    TransportError out, which the HTTP surface turns into a 503, and leaves
    itself exactly as it was. A new message is the caller's to retry, and a
    failed update keeps the message that was already there. The slots already on
    the sign are re-pushed on reconnect by refresh, which is its own path with
    its own tests.
    """

    async def test_a_new_message_is_rejected_and_not_kept(self, registry, transport):
        transport.fail_with = "cable unplugged"

        with pytest.raises(TransportError, match="cable unplugged"):
            await add(registry, "temperature", "18.4")

        assert registry.list_slots() == []

    async def test_a_rejected_write_hands_its_file_back_to_the_pool(
        self, registry, transport
    ):
        # Without the release, a sign down for a while would empty the pool one
        # failed request at a time, and a later assign would raise LayoutFull
        # rather than ever reaching the sign again.
        transport.fail_with = "cable unplugged"
        for index in range(5):
            with pytest.raises(TransportError):
                await add(registry, "slot-%d" % index)

        transport.fail_with = None
        for index in range(3):  # the fixture's pool holds three
            await add(registry, "kept-%d" % index)
        assert len(registry.list_slots()) == 3

    async def test_a_failed_update_leaves_the_previous_message(self, registry, transport):
        await add(registry, "temperature", "18.4")

        transport.fail_with = "cable unplugged"
        with pytest.raises(TransportError):
            await add(registry, "temperature", "20.1")

        assert registry.get("temperature").message == "18.4"

    async def test_a_message_that_cannot_render_is_still_refused(self, registry, transport):
        # Not a link problem, so there is nothing to converge to later.
        transport.fail_with = "cable unplugged"

        with pytest.raises(MessageTooLong):
            await add(registry, "temperature", "X" * 300)

        assert registry.list_slots() == []


class TestRefresh:
    async def test_it_rewrites_everything_even_when_nothing_changed(
        self, registry, transport
    ):
        # The sign can be power cycled behind a still-connected adapter, and
        # suppression would otherwise skip exactly the repairing writes.
        await add(registry, "one")
        await add(registry, "two")
        transport.clear()

        await registry.refresh()

        assert len(payloads_starting(transport, b"A")) == 2
        assert len(run_sequences(transport)) == 1

    async def test_it_never_reallocates_the_sign(self, registry, transport):
        await add(registry, "one")
        transport.clear()

        await registry.refresh()

        assert payloads_starting(transport, b"E$") == []


class TestAFailedWriteGivesTheFileBack:
    """A slot that never existed must not keep the file it was promised.

    The file is assigned before the record that owns it is built, so a failure
    in between leaves it reserved against a key that is in no state. Nothing
    afterwards can find it: a delete needs a slot to delete, and a restart reads
    the state file, which never heard of it.
    """

    async def test_a_failed_write_does_not_spend_a_slot(self, registry, transport):
        transport.fail_with = "cable unplugged"

        for key in ("one", "two", "three"):
            with pytest.raises(TransportError, match="cable unplugged"):
                await add(registry, key)

        assert registry.list_slots() == []
        assert registry.occupancy == (0, 3)

    async def test_the_pool_is_still_there_afterwards(self, registry, transport):
        # The measured consequence: three failures against a three slot pool
        # used to leave it full, so the next good write was refused.
        transport.fail_with = "cable unplugged"
        for key in ("one", "two", "three"):
            with pytest.raises(TransportError):
                await add(registry, key)
        transport.fail_with = None

        await add(registry, "later")

        assert [slot.key for slot in registry.list_slots()] == ["later"]

    async def test_a_slot_that_already_exists_keeps_its_file(self, registry, transport):
        # The other side of it. A failed update must not take the file from the
        # message the sign is still playing.
        await add(registry, "one", "FIRST")
        label = registry.get("one").label
        transport.fail_with = "cable unplugged"

        with pytest.raises(TransportError):
            await add(registry, "one", "SECOND")

        assert registry.get("one").message == "FIRST"
        assert registry.get("one").label == label


class TestACancelledRequestRollsBackToo:
    """A client that gives up mid-write must not cost the pool anything.

    Cancellation raises ``CancelledError``, which is not an ``Exception``, so a
    rollback catching only ``Exception`` never ran for it. FastAPI cancels the
    request task when a client disconnects, so this is an ordinary event rather
    than a theoretical one.
    """

    async def cancel_the_text_write(self, controller):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError

        controller.write_text_file = cancelled

    async def test_a_cancelled_write_does_not_spend_a_slot(self, registry, controller):
        await self.cancel_the_text_write(controller)

        for key in ("one", "two", "three"):
            with pytest.raises(asyncio.CancelledError):
                await add(registry, key)

        assert registry.list_slots() == []
        assert registry.occupancy == (0, 3)

    async def test_a_cancelled_update_leaves_the_message_that_is_showing(
        self, registry, controller
    ):
        await add(registry, "one", "FIRST")
        await self.cancel_the_text_write(controller)

        with pytest.raises(asyncio.CancelledError):
            await add(registry, "one", "SECOND")

        assert registry.get("one").message == "FIRST"

    async def test_a_cancelled_variable_write_is_not_recorded(self, controller, store, state, clock):
        registry = SlotRegistry(controller, Layout(3, 256, 2, 32), store, state, now=clock)
        await registry.restore()

        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError

        controller.write_string_file = cancelled

        with pytest.raises(asyncio.CancelledError):
            await registry.put_variable("temp", "72")

        assert registry.list_variables() == []


class TestReboot:
    """Resetting the sign to recover it, then restoring the rotation.

    Unlike a refresh, which re-pushes content over whatever is there, a reboot
    clears the sign outright and lets it restart before writing anything back.
    The service's own record is what it restores from, so the slots survive and
    come back on the same files.
    """

    async def test_it_clears_the_sign_then_restores_every_slot(
        self, registry, layout, transport, answer_a_pool_reading
    ):
        await add(registry, "one", "ONE")
        await add(registry, "two", "TWO")
        transport.clear()
        answer_a_pool_reading()

        put_the_alert_back = await registry.reboot()

        # The clear and the configuration went out, erasing the sign, ...
        assert frames.packet(frames.clear_memory()) in transport.packets
        assert (
            frames.packet(frames.set_memory_config(layout.allocations()))
            in transport.packets
        )
        # ... and both slots and the run sequence were written again after it.
        assert [slot.key for slot in registry.list_slots()] == ["one", "two"]
        # No alert was up, so the caller is the one that has to put one back,
        # and there was nothing to put.
        assert put_the_alert_back is False
        assert len(payloads_starting(transport, b"A")) == 2
        assert run_sequences(transport)

    async def test_it_keeps_each_slot_on_its_own_file(
        self, registry, layout, transport, answer_a_pool_reading
    ):
        await add(registry, "one")
        await add(registry, "two")
        answer_a_pool_reading()

        await registry.reboot()

        assert layout.slots.label_for("one") == b"A"
        assert layout.slots.label_for("two") == b"B"

    async def test_a_reboot_that_cannot_reach_the_sign_raises(self, registry, transport):
        await add(registry, "one")
        transport.fail_with = "cable unplugged"

        with pytest.raises(TransportError, match="cable unplugged"):
            await registry.reboot()

        # The record is untouched, so the slot is still there to restore once
        # the link is back.
        assert [slot.key for slot in registry.list_slots()] == ["one"]

    async def test_a_sign_without_room_is_refused_before_it_is_cleared(
        self, registry, transport, answer_a_pool_reading
    ):
        # The hole this closes: a sign swapped for a smaller one leaves a state
        # file that still matches the configuration, so startup asks nothing and
        # its own check never runs. Without this the first reboot would erase the
        # sign and write a pool it cannot hold.
        await add(registry, "one")
        transport.clear()
        answer_a_pool_reading(b"0100")

        with pytest.raises(pool.PoolTooLarge, match="the sign has 256"):
            await registry.reboot()

        # The read went out and nothing else did, so the sign still holds what
        # it held and the record is still there to restore from.
        assert transport.packets == [frames.packet(frames.read_general_information())]
        assert [slot.key for slot in registry.list_slots()] == ["one"]

    async def test_a_sign_too_wedged_to_answer_is_rebooted_anyway(
        self, registry, transport
    ):
        # Which is the case the endpoint exists for. The read gets no answer, the
        # assumed pool stands in, and the recovery goes ahead.
        await add(registry, "one")
        transport.clear()

        await registry.reboot()
        assert [slot.key for slot in registry.list_slots()] == ["one"]
        assert frames.packet(frames.clear_memory()) in transport.packets


async def no_wait(seconds: float) -> None:
    await asyncio.sleep(0)


class TestAnInterruptedReboot:
    """A reboot stopped after its clear leaves a record that says so.

    The clear erases every file on the sign, and the configuration after it is
    what gives the sign files to write into. A reboot stopped between the two,
    by a client going away or the link failing, used to leave the state file
    saying the pool was applied and /health saying in sync, so a restart wrote
    every message into files the sign did not have.
    """

    @staticmethod
    def stop_after_the_clear(controller, stop: BaseException) -> None:
        # The settle is the wait between the clear and the configuration, so
        # failing it stops the reboot exactly there.
        async def stopped(seconds: float) -> None:
            raise stop

        controller._sleep = stopped

    @pytest.mark.parametrize(
        "stop", [TransportError("link dropped"), asyncio.CancelledError()], ids=["failed", "cancelled"]
    )
    async def test_a_restart_writes_the_pool_again_and_keeps_every_message(
        self, registry, controller, layout, store, clock, transport, answer_a_pool_reading, stop
    ):
        await add(registry, "one", "ONE")
        await add(registry, "two", "TWO")
        transport.clear()
        answer_a_pool_reading()
        self.stop_after_the_clear(controller, stop)

        with pytest.raises(type(stop)):
            await registry.reboot()

        assert frames.packet(frames.clear_memory()) in transport.packets
        configuration = frames.packet(frames.set_memory_config(layout.allocations()))
        assert configuration not in transport.packets
        assert not registry.in_sync

        # What the next start reads. Setting the layout to None instead would
        # have read as a changed pool, which drops every message from the record.
        saved = store.load()
        assert saved.reallocation_pending is True
        assert saved.layout is not None
        assert sorted(saved.slots) == ["one", "two"]

        fresh = FakeTransport()
        restarted = SlotRegistry(
            SignController(fresh, inter_packet_delay=0, sleep=no_wait),
            Layout(3, 256),
            store,
            saved,
            now=clock,
        )
        await restarted.restore()

        assert frames.packet(frames.clear_memory()) in fresh.packets
        assert configuration in fresh.packets
        assert [slot.key for slot in restarted.list_slots()] == ["one", "two"]
        assert len(payloads_starting(fresh, b"A")) == 2
        assert restarted.in_sync
        assert store.load().reallocation_pending is False

    async def test_a_refresh_does_not_call_it_in_sync(
        self, registry, controller, transport, answer_a_pool_reading
    ):
        # A refresh pushes everything into a sign with no pool, which lands
        # nowhere, and must not then report the sign as matching the record.
        await add(registry, "one")
        answer_a_pool_reading()
        self.stop_after_the_clear(controller, TransportError("link dropped"))
        with pytest.raises(TransportError):
            await registry.reboot()
        controller._sleep = no_wait

        await registry.refresh()

        assert not registry.in_sync

    async def test_a_second_reboot_finishes_it(
        self, registry, controller, store, answer_a_pool_reading
    ):
        await add(registry, "one")
        answer_a_pool_reading()
        self.stop_after_the_clear(controller, TransportError("link dropped"))
        with pytest.raises(TransportError):
            await registry.reboot()
        controller._sleep = no_wait
        answer_a_pool_reading()

        await registry.reboot()

        assert registry.in_sync
        assert store.load().reallocation_pending is False

    async def test_a_reboot_refused_for_room_marks_nothing(
        self, registry, store, transport, answer_a_pool_reading
    ):
        # Refused before the clear, so the sign is as it was and so is the
        # record: nothing is pending and nothing is out of step.
        await add(registry, "one")
        answer_a_pool_reading(b"0100")

        with pytest.raises(pool.PoolTooLarge):
            await registry.reboot()

        assert registry.in_sync
        assert store.load().reallocation_pending is False

    @staticmethod
    def fail_the_first_save_after_the_pool(controller, store, *, every=False) -> list[str]:
        """Make the state file fail at the first save after a pool is written.

        That is the save between the memory configuration and the rewrite, the
        moment the sign is blank. With ``every``, each save after it fails as
        well, which is a disk that stays full. Returns a list that records each
        failure.
        """
        apply_memory_config = controller.apply_memory_config
        save = store.save
        configured = False
        failed: list[str] = []

        async def applying(*args, **kwargs):
            nonlocal configured
            await apply_memory_config(*args, **kwargs)
            configured = True

        def saving(state):
            if configured and (every or not failed):
                failed.append("disk full")
                raise OSError(28, "No space left on device")
            save(state)

        controller.apply_memory_config = applying
        store.save = saving
        return failed

    @staticmethod
    def pending_restart(store, saved, clock) -> tuple[SlotRegistry, FakeTransport]:
        fresh = FakeTransport()
        restarted = SlotRegistry(
            SignController(fresh, inter_packet_delay=0, sleep=no_wait),
            Layout(3, 256),
            store,
            saved,
            now=clock,
        )
        return restarted, fresh

    async def test_a_reboot_whose_save_after_the_pool_fails_still_puts_everything_back(
        self, registry, controller, store, transport, answer_a_pool_reading
    ):
        # The sign is blank once the pool has gone out, and the rewrite is what
        # brings it back, so a full disk there is logged rather than raised.
        await add(registry, "one", "ONE")
        await add(registry, "two", "TWO")
        transport.clear()
        answer_a_pool_reading()
        failed = self.fail_the_first_save_after_the_pool(controller, store)

        await registry.reboot()

        assert failed
        assert len(payloads_starting(transport, b"A")) == 2
        assert registry.in_sync
        # The save after the rewrite went through, so the file no longer says
        # the pool is pending and the next start writes nothing.
        assert store.load().reallocation_pending is False

    async def test_a_reboot_whose_every_save_after_the_pool_fails_leaves_it_pending(
        self, registry, controller, store, transport, answer_a_pool_reading
    ):
        # A disk that stays full. The display still comes back, and the file
        # still says the pool is pending, so the next start writes it once more.
        await add(registry, "one", "ONE")
        transport.clear()
        answer_a_pool_reading()
        failed = self.fail_the_first_save_after_the_pool(controller, store, every=True)

        with pytest.raises(OSError):
            await registry.reboot()

        assert len(failed) == 2
        assert len(payloads_starting(transport, b"A")) == 1
        assert store.load().reallocation_pending is True

    async def test_a_start_whose_save_after_the_pool_fails_still_puts_everything_back(
        self, registry, store, state, clock
    ):
        await add(registry, "one", "ONE")
        state.reallocation_pending = True
        store.save(state)
        restarted, fresh = self.pending_restart(store, store.load(), clock)
        failed = self.fail_the_first_save_after_the_pool(restarted._controller, store)

        await restarted.restore()

        assert failed
        assert len(payloads_starting(fresh, b"A")) == 1
        assert restarted.in_sync
        assert store.load().reallocation_pending is False

    async def test_a_start_that_stops_after_the_pool_reports_the_sign_out_of_step(
        self, registry, store, state, clock
    ):
        # The pool went out, so the sign is blank, and nothing was put back.
        # The service starts anyway after a failed restore, and must not then
        # call that sign in sync.
        await add(registry, "one", "ONE")
        state.reallocation_pending = True
        store.save(state)
        restarted, fresh = self.pending_restart(store, store.load(), clock)

        async def unplugged(*args, **kwargs):
            raise TransportError("cable unplugged")

        restarted._controller.write_text_file = unplugged
        with pytest.raises(TransportError):
            await restarted.restore()

        assert frames.packet(frames.clear_memory()) in fresh.packets
        assert not restarted.in_sync

    def test_a_state_file_from_before_reads_as_nothing_pending(self, tmp_path):
        path = tmp_path / "state.json"
        StateStore(path).save(ServiceState())
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["reallocation_pending"]
        path.write_text(json.dumps(data), encoding="utf-8")

        assert StateStore(path).load().reallocation_pending is False


class TestAnOwedRestore:
    """A start that had to write the sign's file pool and could not reach the sign.

    Issue #118. The records were left naming files the layout had not been told
    about, so the next write was handed a file a restored record still held, and
    nothing ever wrote the pool. Now the restore stays owed: every write that
    needs the sign's files is refused before it touches the sign, /health says
    the sign is out of step, and the first reconnect finishes it, asking the
    sign for its room first.
    """

    @staticmethod
    async def a_previous_run(store, clock) -> ServiceState:
        """Leave a state file holding a variable in ``a`` and a slot calling it."""
        state = ServiceState()
        registry = SlotRegistry(
            SignController(FakeTransport(), inter_packet_delay=0, sleep=no_wait),
            Layout(3, 256, 2, 32),
            store,
            state,
            now=clock,
        )
        await registry.restore()
        await registry.put_variable("temp", "72")
        await add(registry, "one", "<var:temp>")
        return state

    @staticmethod
    async def a_start_with_the_sign_out_of_reach(
        store, clock, transport, layout
    ) -> SlotRegistry:
        transport.open_fails_with = "no route to host"
        restarted = SlotRegistry(
            SignController(transport, inter_packet_delay=0, sleep=no_wait),
            layout,
            store,
            store.load(),
            now=clock,
        )
        with pytest.raises(TransportError):
            await restarted.restore()
        return restarted

    async def test_a_changed_pool_refuses_writes_that_need_files_until_it_is_written(
        self, store, clock, transport, answer_a_pool_reading
    ):
        await self.a_previous_run(store, clock)
        # One more slot, so the pool has changed and the start must write it.
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        assert restarted.restore_owed
        assert not restarted.in_sync

        # The adapter is back, and writes arrive before the reconnect hook has
        # run. Each would have been handed a file a restored record holds.
        transport.open_fails_with = None
        with pytest.raises(SignNotRestored):
            await restarted.put_variable("humidity", "40")
        with pytest.raises(SignNotRestored):
            await add(restarted, "two", "TWO")
        with pytest.raises(SignNotRestored):
            await restarted.set_active("one", False)
        with pytest.raises(SignNotRestored):
            await restarted.remove("one")
        with pytest.raises(SignNotRestored):
            await restarted.remove_variable("temp")
        with pytest.raises(SignNotRestored):
            await restarted.refresh()
        with pytest.raises(SignNotRestored):
            await restarted.clear()
        with pytest.raises(SignNotRestored):
            async with restarted.rendering("FIRE"):
                pass
        assert await restarted.sweep() == []
        assert transport.packets == []

        answer_a_pool_reading()
        await restarted.finish_owed_restore()

        assert frames.packet(frames.clear_memory()) in transport.packets
        assert not restarted.restore_owed
        assert restarted.in_sync
        # A changed pool empties the record when it is written, as it always
        # has, and the start is recorded as having applied it.
        assert restarted.list_slots() == []
        assert not Layout(4, 256, 2, 32).needs_reconfiguration(store.load().layout)
        await restarted.put_variable("humidity", "40")

    async def test_a_pending_reallocation_keeps_its_records_on_their_files(
        self, store, clock, transport, answer_a_pool_reading
    ):
        state = await self.a_previous_run(store, clock)
        state.reallocation_pending = True
        store.save(state)
        layout = Layout(3, 256, 2, 32)

        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, layout
        )

        # The pool has not changed and the records survive its write, so they
        # hold their files while the restore is owed.
        assert restarted.restore_owed
        assert layout.variables.label_for("temp") == b"a"
        assert layout.slots.label_for("one") == b"A"

        transport.open_fails_with = None
        answer_a_pool_reading()
        await restarted.finish_owed_restore()

        assert frames.packet(frames.clear_memory()) in transport.packets
        assert [slot.key for slot in restarted.list_slots()] == ["one"]
        assert [variable.name for variable in restarted.list_variables()] == ["temp"]
        assert restarted.in_sync
        assert store.load().reallocation_pending is False

    async def test_a_reboot_while_it_is_owed_finishes_it(
        self, store, clock, transport, answer_a_pool_reading
    ):
        # Issue #118's step 6: a reboot that went its own way recorded the new
        # pool as applied while the records still named the old one's files.
        await self.a_previous_run(store, clock)
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        transport.open_fails_with = None
        answer_a_pool_reading()

        await restarted.reboot()

        assert not restarted.restore_owed
        assert restarted.list_slots() == []
        assert store.load().slots == {}
        assert not Layout(4, 256, 2, 32).needs_reconfiguration(store.load().layout)

    async def test_a_sign_without_room_is_left_alone_and_it_stays_owed(
        self, store, clock, transport, answer_a_pool_reading
    ):
        await self.a_previous_run(store, clock)
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        transport.open_fails_with = None
        answer_a_pool_reading(b"0100")

        with pytest.raises(pool.PoolTooLarge):
            await restarted.finish_owed_restore()

        assert transport.packets == [frames.packet(frames.read_general_information())]
        assert restarted.restore_owed
        # A refused write now says why, rather than sending the caller to wait
        # for a reconnect that has already happened.
        with pytest.raises(SignNotRestored, match="the sign has 256") as refused:
            await add(restarted, "two", "TWO")
        assert "next reaches the sign" not in str(refused.value)

    async def test_a_waiting_alert_keeps_its_place_while_it_is_owed(
        self, store, clock, transport
    ):
        # Its turn comes and nothing can be drawn yet. That is the sign not
        # being ready, not the alert being unshowable, so it is not dropped,
        # and the sweep that tries it every second is not told it failed.
        await self.a_previous_run(store, clock)
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        transport.open_fails_with = None
        transport.ensure_open()
        state = restarted._state
        state.alert_queue.append(QueuedAlertState(message="NEXT", mode="HOLD", queued_at=clock()))
        alerts = AlertService(restarted._controller, store, state, now=clock)
        alerts.set_rendering(restarted.rendering)

        assert await alerts.promote() is False

        assert [waiting.message for waiting in alerts.waiting] == ["NEXT"]
        assert transport.packets == []

    async def test_an_expiry_waits_for_it(self, store, clock, transport):
        # Expiring a slot writes the run sequence and frees its file, neither
        # of which can be done before the pool has been. The sweep runs every
        # second, so it waits quietly rather than failing each time.
        state = await self.a_previous_run(store, clock)
        state.slots["one"] = state.slots["one"].model_copy(
            update={"expires_at": clock() + timedelta(seconds=10)}
        )
        store.save(state)
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        transport.open_fails_with = None
        clock.advance(11)

        assert await restarted.sweep() == []

        assert [slot.key for slot in restarted.list_slots()] == ["one"]
        assert transport.packets == []

    async def an_alert_request_during_the_finish(
        self, store, clock, transport, answer_a_pool_reading, request
    ):
        """Finish an owed restore with an alert recorded, and ``request`` arriving meanwhile.

        The request reaches the registry's lock while the finish holds it,
        writing the new pool, which is the moment a real one waits for seconds
        through the settle. Gated on events rather than on counting the loop's
        turns, which differ between Python versions.
        """
        state = await self.a_previous_run(store, clock)
        before = AlertService(
            SignController(FakeTransport(), inter_packet_delay=0, sleep=no_wait),
            store,
            state,
            now=clock,
        )
        await before.raise_alert("OLD ALERT", mode="HOLD")
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        alerts = AlertService(restarted._controller, store, restarted._state, now=clock)
        alerts.set_rendering(restarted.rendering)
        restarted.set_priority_hold(alerts.lifted)
        transport.open_fails_with = None
        answer_a_pool_reading()

        writing, go_on = asyncio.Event(), asyncio.Event()
        apply_memory_config = restarted._controller.apply_memory_config

        async def held(*args, **kwargs):
            writing.set()
            await go_on.wait()
            await apply_memory_config(*args, **kwargs)

        restarted._controller.apply_memory_config = held
        finishing = asyncio.create_task(_finish_the_startup_restore(restarted, alerts))
        await asyncio.wait_for(writing.wait(), 5)
        requesting = asyncio.create_task(request(alerts))
        async with asyncio.timeout(5):
            while not restarted._lock._waiters:
                await asyncio.sleep(0)
        go_on.set()
        await asyncio.wait_for(asyncio.gather(finishing, requesting), 5)
        return alerts

    @staticmethod
    def last_priority_write(transport) -> bytes:
        prefix = frames.packet(b"")[: -len(b"")]
        writes = [
            packet[len(prefix) :]
            for packet in transport.packets
            if packet.startswith(prefix + b"A0")
        ]
        return writes[-1]

    async def test_an_alert_raised_during_the_finish_is_not_written_over(
        self, store, clock, transport, answer_a_pool_reading
    ):
        # The finish puts the recorded alert back once the pool is written. An
        # alert raised while it held the lock got in first, and the old one,
        # read before that, used to go out over it.
        async def replace(alerts):
            await alerts.raise_alert("NEW ALERT", mode="HOLD")

        alerts = await self.an_alert_request_during_the_finish(
            store, clock, transport, answer_a_pool_reading, replace
        )

        assert alerts.active.message == "NEW ALERT"
        assert b"NEW ALERT" in self.last_priority_write(transport)

    async def test_an_alert_dismissed_during_the_finish_stays_dismissed(
        self, store, clock, transport, answer_a_pool_reading
    ):
        async def dismiss(alerts):
            await alerts.dismiss()

        alerts = await self.an_alert_request_during_the_finish(
            store, clock, transport, answer_a_pool_reading, dismiss
        )

        assert alerts.active is None
        assert b"OLD ALERT" not in self.last_priority_write(transport)

    async def test_a_shrunk_pool_keeps_its_records_until_the_pool_is_written(
        self, store, clock, transport, answer_a_pool_reading
    ):
        # The records name files of the old pool, and a smaller pool has no
        # file C. Reattaching them before the write, as the issue suggested,
        # would drop the slot in C from the record while the sign, unreached,
        # still shows it. They are kept until the new pool goes out, which
        # empties the record as a changed pool always has.
        state = await self.a_previous_run(store, clock)
        for key in ("two", "three"):
            restarted_before = SlotRegistry(
                SignController(FakeTransport(), inter_packet_delay=0, sleep=no_wait),
                Layout(3, 256, 2, 32),
                store,
                state,
                now=clock,
            )
            await restarted_before.restore()
            await add(restarted_before, key, key.upper())
        assert sorted(store.load().slots) == ["one", "three", "two"]

        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(2, 256, 2, 32)
        )

        assert sorted(slot.key for slot in restarted.list_slots()) == ["one", "three", "two"]

        transport.open_fails_with = None
        answer_a_pool_reading()
        await restarted.finish_owed_restore()

        assert restarted.list_slots() == []

    async def test_the_reconnect_finishes_it_instead_of_refreshing(
        self, store, clock, transport, answer_a_pool_reading
    ):
        # The alert's half of the restore runs too, and the usual refresh does
        # not follow it: on a sign with no icons that refresh re-asserts the
        # alert, forced, which restarts it on the display a second time.
        state = await self.a_previous_run(store, clock)
        before = AlertService(
            SignController(FakeTransport(), inter_packet_delay=0, sleep=no_wait),
            store,
            state,
            now=clock,
        )
        await before.raise_alert("FIRE", mode="HOLD")
        restarted = await self.a_start_with_the_sign_out_of_reach(
            store, clock, transport, Layout(4, 256, 2, 32)
        )
        alerts = AlertService(restarted._controller, store, restarted._state, now=clock)
        alerts.set_rendering(restarted.rendering)
        restarted.set_priority_hold(alerts.lifted)
        transport.open_fails_with = None
        answer_a_pool_reading()

        await _after_reconnect(restarted, alerts)

        assert not restarted.restore_owed
        assert alerts.active.message == "FIRE"
        assert len([packet for packet in transport.packets if b"FIRE" in packet]) == 1

    async def test_nothing_owed_means_nothing_is_written(self, registry, transport):
        # The case that matters most: an ordinary reconnect must never reach
        # the erase.
        await add(registry, "one")
        transport.clear()

        await registry.finish_owed_restore()

        assert transport.packets == []


class TestAnAlertDoesNotHoldTheRunSequenceBack:
    """The run sequence goes out whatever is on the sign, an alert included.

    The service used to hold these writes back, because the protocol says a
    running priority message is cancelled by a write to the run time or run day
    table and says nothing either way about the run sequence. The spike settled
    it on 2026-09-11: a real sequence change with an alert up left the alert on
    the sign, so there is nothing to defer. These pin that it stays gone.
    """

    def registry_with_an_alert(self, controller, layout, store, state, clock):
        state.alert = AlertState(message="ALERT", mode="HOLD", started_at=clock())
        return SlotRegistry(controller, layout, store, state, now=clock)

    async def test_a_new_slot_reaches_the_run_sequence_during_an_alert(
        self, controller, layout, store, state, clock, transport
    ):
        registry = self.registry_with_an_alert(controller, layout, store, state, clock)
        await registry.restore()
        transport.clear()

        await add(registry, "temperature")

        assert len(run_sequences(transport)) == 1

    async def test_the_message_itself_still_reaches_the_sign(
        self, controller, layout, store, state, clock, transport
    ):
        # Writing a TEXT file was never on the protocol's list either, so
        # content stays current behind the alert.
        registry = self.registry_with_an_alert(controller, layout, store, state, clock)
        await registry.restore()
        transport.clear()

        await add(registry, "temperature", "18.4")

        assert payloads_starting(transport, b"A")

    async def test_restore_configures_the_rotation_during_an_alert(
        self, controller, layout, store, state, clock, transport
    ):
        registry = self.registry_with_an_alert(controller, layout, store, state, clock)

        await registry.restore()

        assert len(run_sequences(transport)) == 1
