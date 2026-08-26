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
from readerboard.transport.fake import FakeTransport


async def add(registry, key, message="HI", **kwargs):
    return await registry.upsert(key, message, mode="HOLD", position="MIDDLE", **kwargs)


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


class TestRestart:
    def rebuild(self, store, transport, clock, slot_count=3, slot_capacity=256):
        """Build a second registry over the same state file, as a restart would."""
        controller = SignController(transport, inter_packet_delay=0)
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

        assert layout.label_for("one") == b"A"
        assert layout.label_for("two") == b"B"
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
    """A validated write is a request the registry can satisfy on its own.

    The registry is the durable record of what should be on the sign, so it
    accepts the message, persists it, and converges when the link comes back.
    Returning 503 here would make it a pass-through and push the retry logic
    back onto Home Assistant, which is the thing this rewrite exists to take
    off it.
    """

    async def test_the_slot_is_accepted_and_kept(self, registry, transport):
        transport.fail_with = "cable unplugged"

        slot = await add(registry, "temperature", "18.4")

        assert slot.key == "temperature"
        assert [s.key for s in registry.list_slots()] == ["temperature"]

    async def test_health_can_tell_that_the_sign_is_behind(self, registry, transport):
        assert registry.in_sync

        transport.fail_with = "cable unplugged"
        await add(registry, "temperature")

        assert not registry.in_sync

    async def test_it_is_persisted_so_a_restart_does_not_lose_it(
        self, registry, transport, store
    ):
        transport.fail_with = "cable unplugged"
        await add(registry, "temperature", "18.4")

        assert "temperature" in store.path.read_text(encoding="utf-8")

    async def test_it_reaches_the_sign_once_the_link_is_back(self, registry, transport):
        transport.fail_with = "cable unplugged"
        await add(registry, "temperature", "18.4")

        transport.fail_with = None
        transport.clear()
        await registry.refresh()

        assert registry.in_sync
        assert transport.packets

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
