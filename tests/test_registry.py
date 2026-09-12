"""Tests for the registry: upsert, ordering, TTL, capacity, and restart."""

import pytest

from readerboard.protocol import frames
from readerboard.services.registry import (
    LayoutFull,
    MessageRegistry,
    MessageTooLong,
    UnknownSlot,
)
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
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
        # The measurement this is built on: a sequence write leaves the rotation
        # running, and rewriting a TEXT file restarts the message in it. So
        # hiding is the sequence and nothing else, or it blinks for nothing.
        await add(registry, "one", "HELLO")
        await add(registry, "two")
        transport.clear()

        await registry.set_active("one", False)

        assert len(run_sequences(transport)) == 1
        assert payloads_starting(transport, b"A") == []
        assert payloads_starting(transport, b"B") == []

    async def test_hiding_one_leaves_its_text_on_the_sign(self, registry, transport):
        # Nothing cycles to it, so the bytes sit there unseen. That is what
        # makes showing it again free.
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
        restored = MessageRegistry(controller, Layout(3, 256), store, store.load(), now=clock)
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
        restored = MessageRegistry(controller, Layout(3, 256), store, store.load(), now=clock)
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
        return MessageRegistry(controller, layout, store, state, now=clock), layout

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


class TestReboot:
    """Resetting the sign to recover it, then restoring the rotation.

    Unlike a refresh, which re-pushes content over whatever is there, a reboot
    clears the sign outright and lets it restart before writing anything back.
    The service's own record is what it restores from, so the slots survive and
    come back on the same files.
    """

    async def test_it_clears_the_sign_then_restores_every_slot(
        self, registry, layout, transport
    ):
        await add(registry, "one", "ONE")
        await add(registry, "two", "TWO")
        transport.clear()

        restored = await registry.reboot()

        # The clear and the configuration went out, erasing the sign, ...
        assert frames.packet(frames.clear_memory()) in transport.packets
        assert (
            frames.packet(frames.set_memory_config(layout.allocations()))
            in transport.packets
        )
        # ... and both slots and the run sequence were written again after it.
        assert restored == 2
        assert [slot.key for slot in registry.list_slots()] == ["one", "two"]
        assert len(payloads_starting(transport, b"A")) == 2
        assert run_sequences(transport)

    async def test_it_keeps_each_slot_on_its_own_file(self, registry, layout):
        await add(registry, "one")
        await add(registry, "two")

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


class TestAlertDeferral:
    """A run sequence write during an alert might cancel it.

    The protocol says a running priority message is cancelled by a write to the
    run time or run day table, and says nothing either way about the run
    sequence. Until the spike settles that, the safe reading is that it might.
    """

    def registry_with_alert(self, controller, layout, store, state, clock, active):
        return MessageRegistry(
            controller, layout, store, state, now=clock, alert_active=lambda: active()
        )

    async def test_the_run_sequence_is_held_back_while_an_alert_is_up(
        self, controller, layout, store, state, clock, transport
    ):
        holding = True
        registry = self.registry_with_alert(
            controller, layout, store, state, clock, lambda: holding
        )
        await registry.restore()
        transport.clear()

        await add(registry, "temperature")

        assert run_sequences(transport) == []

    async def test_the_message_itself_still_reaches_the_sign(
        self, controller, layout, store, state, clock, transport
    ):
        # Writing a TEXT file is not on the protocol's list of things that
        # cancel a priority message, so content stays current behind the alert.
        registry = self.registry_with_alert(
            controller, layout, store, state, clock, lambda: True
        )
        await registry.restore()
        transport.clear()

        await add(registry, "temperature", "18.4")

        assert payloads_starting(transport, b"A")

    async def test_it_is_applied_when_the_alert_is_released(
        self, controller, layout, store, state, clock, transport
    ):
        holding = True
        registry = self.registry_with_alert(
            controller, layout, store, state, clock, lambda: holding
        )
        await registry.restore()
        await add(registry, "temperature")
        transport.clear()

        holding = False
        assert await registry.flush_deferred() is True

        assert len(run_sequences(transport)) == 1

    async def test_flushing_with_nothing_deferred_does_nothing(self, registry):
        assert await registry.flush_deferred() is False

    async def test_restore_applies_the_sequence_even_if_state_says_alert(
        self, controller, layout, store, state, clock, transport
    ):
        # At startup the alert has not been re-asserted on the sign yet, so
        # there is nothing to protect and the rotation must be configured.
        registry = self.registry_with_alert(
            controller, layout, store, state, clock, lambda: True
        )
        await registry.restore()

        assert len(run_sequences(transport)) == 1
