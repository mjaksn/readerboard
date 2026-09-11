"""Tests for variables: values in STRING files that messages call by name.

What makes variables worth having is that changing one writes one small STRING
file and nothing else, so the message calling it neither blanks nor restarts.
What makes them dangerous is the label: it is written into every calling
message as raw bytes, so a variable deleted while called, and its file handed to
another, would put the wrong value on the sign with nothing to say so. Most of
what follows is one or the other.
"""

from __future__ import annotations

import logging

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import MarkupError
from readerboard.services.registry import (
    LayoutFull,
    MessageRegistry,
    UnknownVariable,
    VariableInUse,
    VariablesDisabled,
    VariableTooLong,
)
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
from readerboard.sign.state import ServiceState
from readerboard.transport.base import TransportError
from readerboard.transport.fake import FakeTransport

FRAME_PREFIX = frames.packet(b"")[: -len(c.EOT)]


@pytest.fixture
def layout() -> Layout:
    # Three slots and three variables of sixteen bytes, small enough to fill.
    return Layout(3, 256, 3, 16)


def payloads(transport: FakeTransport) -> list[bytes]:
    """Return every payload the transport saw, framing stripped, in order."""
    return [
        packet[len(FRAME_PREFIX) : -len(c.EOT)]
        for packet in transport.packets
        if packet.startswith(FRAME_PREFIX)
    ]


def commands(transport: FakeTransport) -> list[bytes]:
    """Return the command byte of every payload, so an order of writes can be asserted."""
    return [payload[:1] for payload in payloads(transport)]


async def put(registry: MessageRegistry, name: str, value: str = "72", **kwargs):
    return await registry.put_variable(name, value, **kwargs)


async def add(registry: MessageRegistry, key: str, message: str, **kwargs):
    return await registry.upsert(key, message, mode="HOLD", **kwargs)


class TestPut:
    async def test_a_new_variable_gets_a_string_file_and_its_value(self, registry, transport):
        variable = await put(registry, "temp", "72")

        assert variable.label == "a"
        assert payloads(transport)[-1] == b"Ga72"

    async def test_changing_a_value_writes_only_its_string_file(self, registry, transport):
        await put(registry, "temp", "72")
        await add(registry, "weather", "T=<var:temp>F")
        transport.clear()

        await put(registry, "temp", "73")

        # One STRING write and nothing else: no TEXT rewrite to blank the display,
        # and no run sequence.
        assert payloads(transport) == [b"Ga73"]

    async def test_an_unchanged_value_is_not_sent_again(self, registry, transport):
        await put(registry, "temp", "72")
        transport.clear()
        await put(registry, "temp", "72")
        assert payloads(transport) == []

    async def test_a_value_takes_markup(self, registry, transport):
        await put(registry, "status", "<red>DOWN")
        assert payloads(transport)[-1] == b"Ga" + c.TEXT_COLOR_RED + b"DOWN"

    async def test_an_empty_value_is_allowed_and_shows_nothing(self, registry, transport):
        await put(registry, "temp", "")
        assert payloads(transport)[-1] == b"Ga"

    async def test_a_value_too_long_for_its_file_is_refused(self, registry):
        # The sign empties a value that overruns its file rather than cutting it
        # short, so this has to be refused here.
        with pytest.raises(VariableTooLong, match="empties it"):
            await put(registry, "temp", "X" * 17)
        assert registry.variable_occupancy == (0, 3)

    async def test_a_stale_value_too_long_is_refused_when_given(self, registry):
        with pytest.raises(VariableTooLong, match="stale_value"):
            await put(registry, "temp", "72", stale_value="X" * 17)

    async def test_the_day_of_week_is_refused_in_a_value(self, registry):
        with pytest.raises(MarkupError, match="literal character"):
            await put(registry, "day", "<week_day>")

    async def test_a_full_pool_refuses_in_its_own_words(self, registry):
        for name in ("a", "b", "c"):
            await put(registry, name)
        with pytest.raises(LayoutFull, match="all 3 variables are in use"):
            await put(registry, "d")

    async def test_a_failed_write_records_nothing(self, registry, transport):
        transport.fail_with = "cable unplugged"
        with pytest.raises(TransportError):
            await put(registry, "temp", "72")
        transport.fail_with = None

        assert registry.list_variables() == []
        assert registry.variable_occupancy == (0, 3)

    async def test_a_failed_update_keeps_the_previous_value(self, registry, transport):
        await put(registry, "temp", "72")
        transport.fail_with = "cable unplugged"
        with pytest.raises(TransportError):
            await put(registry, "temp", "73")
        transport.fail_with = None

        assert registry.get_variable("temp").value == "72"


class TestCalling:
    async def test_a_call_renders_as_the_call_code_and_the_label(self, registry, transport):
        await put(registry, "temp", "72")
        await add(registry, "weather", "T=<var:temp>F")

        text = [payload for payload in payloads(transport) if payload.startswith(b"AA")][-1]
        assert text.endswith(b"T=" + c.STRING_FILE_INSERT + b"aF")

    async def test_a_message_calling_a_variable_that_does_not_exist_is_refused(self, registry):
        with pytest.raises(MarkupError, match="no variable named 'temp'"):
            await add(registry, "weather", "T=<var:temp>")
        assert registry.list_slots() == []

    async def test_callers_are_listed_in_rotation_order(self, registry):
        await put(registry, "temp")
        await add(registry, "second", "<var:temp>", order=2)
        await add(registry, "first", "<var:temp>", order=1)
        await add(registry, "other", "no call")

        assert registry.callers("temp") == ["first", "second"]


class TestRemoval:
    async def test_a_variable_a_message_calls_cannot_be_deleted(self, registry):
        await put(registry, "temp")
        await add(registry, "weather", "T=<var:temp>")

        with pytest.raises(VariableInUse, match="slot 'weather'"):
            await registry.remove_variable("temp")
        assert registry.get_variable("temp").label == "a"

    async def test_once_nothing_calls_it_it_can_go(self, registry):
        await put(registry, "temp")
        await add(registry, "weather", "T=<var:temp>")
        await add(registry, "weather", "no call")

        await registry.remove_variable("temp")
        with pytest.raises(UnknownVariable):
            registry.get_variable("temp")

    async def test_deleting_empties_the_file(self, registry, transport):
        await put(registry, "temp", "72")
        await registry.remove_variable("temp")
        assert payloads(transport)[-1] == b"Ga"

    async def test_the_next_variable_in_that_file_is_not_suppressed(self, registry, transport):
        # Without the emptying above, the cache would still believe file a holds
        # 72, and a new variable's first value of 72 would never be sent.
        await put(registry, "old", "72")
        await registry.remove_variable("old")
        await put(registry, "new", "72")

        assert payloads(transport).count(b"Ga72") == 2

    async def test_deleting_one_that_does_not_exist_is_a_clean_error(self, registry):
        with pytest.raises(UnknownVariable, match="no variable named"):
            await registry.remove_variable("nobody")


class TestSwitchedOff:
    @pytest.fixture
    def layout(self) -> Layout:
        return Layout(3, 256, 0, 16)

    async def test_a_variable_cannot_be_created(self, registry):
        with pytest.raises(VariablesDisabled, match="variable_count is 0"):
            await put(registry, "temp")

    async def test_a_message_calling_one_says_why(self, registry):
        with pytest.raises(VariablesDisabled, match="variable_count is 0"):
            await add(registry, "weather", "<var:temp>")

    async def test_other_messages_are_unaffected(self, registry):
        assert (await add(registry, "weather", "plain")).label == "A"


class TestStaleness:
    async def test_an_expired_value_shows_its_stale_value(self, registry, transport, clock):
        await put(registry, "temp", "72", ttl_seconds=60, stale_value="--")

        clock.advance(59)
        await registry.sweep()
        assert not registry.get_variable("temp").stale

        clock.advance(2)
        await registry.sweep()
        variable = registry.get_variable("temp")
        assert variable.stale
        assert variable.expires_at is None
        assert payloads(transport)[-1] == b"Ga--"

    async def test_expiry_never_deletes_a_variable(self, registry, clock):
        await put(registry, "temp", "72", ttl_seconds=60)
        await add(registry, "weather", "<var:temp>")

        clock.advance(61)
        assert await registry.sweep() == []
        assert registry.get_variable("temp").label == "a"

    async def test_with_no_stale_value_it_shows_nothing(self, registry, transport, clock):
        await put(registry, "temp", "72", ttl_seconds=60)
        clock.advance(61)
        await registry.sweep()
        assert payloads(transport)[-1] == b"Ga"

    async def test_a_fresh_value_clears_the_stale_flag(self, registry, clock):
        await put(registry, "temp", "72", ttl_seconds=60, stale_value="--")
        clock.advance(61)
        await registry.sweep()

        variable = await put(registry, "temp", "73", ttl_seconds=60, stale_value="--")
        assert not variable.stale
        assert variable.expires_at is not None

    async def test_a_value_without_a_ttl_never_goes_stale(self, registry, clock):
        await put(registry, "temp", "72")
        clock.advance(86400)
        await registry.sweep()
        assert not registry.get_variable("temp").stale


class TestOrderOfWrites:
    """Variables go first, so a message is never drawn calling an unwritten STRING."""

    async def test_a_refresh_writes_variables_then_messages_then_the_run_sequence(
        self, registry, transport
    ):
        await put(registry, "temp", "72")
        await add(registry, "weather", "T=<var:temp>")
        transport.clear()

        await registry.refresh()

        assert commands(transport) == [b"G", b"A", b"E"]

    async def test_a_reboot_does_the_same_after_the_clear(self, registry, transport):
        await put(registry, "temp", "72")
        await add(registry, "weather", "T=<var:temp>")
        transport.clear()

        await registry.reboot()

        # The clear, the memory configuration, then the same order as a refresh.
        assert commands(transport) == [b"E", b"E", b"G", b"A", b"E"]
        assert payloads(transport)[1].endswith(b"aBL00100000bBL00100000cBL00100000")


class TestRestart:
    def rebuild(self, store, transport, clock, layout):
        controller = SignController(transport, inter_packet_delay=0, settle=False)
        return MessageRegistry(controller, layout, store, store.load(), now=clock)

    async def test_variables_come_back_in_the_same_files(self, registry, store, transport, clock):
        await put(registry, "temp", "72")
        await put(registry, "wind", "5")

        restored = self.rebuild(store, transport, clock, Layout(3, 256, 3, 16))
        await restored.restore()

        assert [(v.name, v.label) for v in restored.list_variables()] == [
            ("temp", "a"),
            ("wind", "b"),
        ]

    async def test_a_new_variable_after_restart_does_not_take_a_restored_file(
        self, registry, store, transport, clock
    ):
        await put(registry, "temp", "72")

        restored = self.rebuild(store, transport, clock, Layout(3, 256, 3, 16))
        await restored.restore()

        assert (await put(restored, "wind", "5")).label == "b"

    async def test_changing_the_variable_pool_reallocates_and_starts_empty(
        self, registry, store, transport, clock
    ):
        await put(registry, "temp", "72")
        await add(registry, "weather", "plain")

        restored = self.rebuild(store, transport, clock, Layout(3, 256, 4, 16))
        await restored.restore()

        assert restored.list_variables() == []
        assert restored.list_slots() == []

    async def test_a_slot_calling_a_variable_that_did_not_come_back_stays(
        self, store, transport, clock, caplog
    ):
        # A state file whose variable sits outside the pool as it now stands,
        # with a slot calling it. Built by hand, since the service itself never
        # reaches this without a reallocation that would clear both.
        layout = Layout(3, 256, 3, 16)
        state = ServiceState.model_validate(
            {
                "layout": layout.as_applied().model_dump(),
                "variables": {
                    "temp": {"name": "temp", "label": "z", "value": "72", "updated_at": clock()}
                },
                "slots": {
                    "weather": {
                        "key": "weather",
                        "label": "A",
                        "message": "[<var:temp>]",
                        "mode": "HOLD",
                        "updated_at": clock(),
                    }
                },
            }
        )
        store.save(state)

        restored = self.rebuild(store, transport, clock, layout)
        with caplog.at_level(logging.WARNING):
            await restored.restore()

        assert restored.list_variables() == []
        assert [slot.key for slot in restored.list_slots()] == ["weather"]
        assert "calls variable 'temp', which no longer exists" in caplog.text
        # The call draws nothing, which is what the sign would draw for it.
        assert any(payload.endswith(b"[]") for payload in payloads(transport))
