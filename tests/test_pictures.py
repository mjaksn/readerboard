"""Tests for icons: bitmaps in picture files that messages draw with <icon:name>.

A picture is unlike the other two kinds of file on the sign, and nearly every
test here is about one of the two ways it differs.

It is claimed rather than created. Nobody registers an icon; a file is taken by
whichever icon a message happens to call, and `picture_count` says how many can
be on the sign at once out of the hundred and forty eight there are.

And writing one blanks the display. That is measured, not feared, and it is why
a file is kept after its last caller goes rather than freed: the common case,
two sources alternating between two icons, must cost nothing. It is also why a
hidden slot and the alert hold their icons as firmly as a visible message does.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from readerboard import icons
from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.services.alerts import AlertAlreadyActive, AlertService, AlertTooLong
from readerboard.services.registry import (
    IconsDisabled,
    PicturePoolFull,
    SlotRegistry,
)
from readerboard.sign.controller import SignController
from readerboard.sign.layout import PICTURE_COLUMNS, PICTURE_ROWS, Layout
from readerboard.transport.base import TransportError
from readerboard.transport.fake import FakeTransport

FRAME_PREFIX = frames.packet(b"")[: -len(c.EOT)]


@pytest.fixture
def layout() -> Layout:
    # Three slots and two picture files, few enough that the pool can be filled
    # in a test without a page of setup.
    return Layout(3, 256, 0, 32, 2)


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


def picture_writes(transport: FakeTransport) -> list[bytes]:
    """Return just the picture writes, which are the ones that blank the display."""
    return [payload for payload in payloads(transport) if payload[:1] == c.COMMAND_WRITE_DOTS]


async def add(registry: SlotRegistry, key: str, message: str, **kwargs):
    return await registry.upsert(key, message, mode="HOLD", **kwargs)


def priority_writes(transport: FakeTransport) -> list[bytes]:
    """Return every write to the priority file, releases included, in order.

    A release is a bare write carrying the label and nothing else, so it is
    shorter than the two bytes a takeover needs; :func:`is_release` reads that.
    """
    return [
        payload
        for payload in payloads(transport)
        if payload[:1] == c.COMMAND_WRITE_TEXT and payload[1:2] == c.FILE_PRIORITY
    ]


def is_release(payload: bytes) -> bool:
    """Whether a priority write is the bare one that hands the sign back."""
    return payload == c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY


@pytest.fixture
def wired(registry: SlotRegistry, alerts: AlertService) -> tuple[SlotRegistry, AlertService]:
    """Wire the two services to each other exactly as ``app.py`` does.

    Both directions, which is the point. The registry renders an alert so a
    variable it calls cannot be deleted underneath it, and the alert service
    hands the sign back so the registry can write a picture. Wiring only the
    first is what the rest of this file does, and it is why none of it sees the
    priority file move.
    """
    alerts.set_rendering(registry.rendering)
    registry.set_priority_hold(alerts.lifted)
    return registry, alerts


class TestAllocating:
    def test_a_picture_file_is_allocated_as_eight_colour_at_one_fixed_size(self, layout):
        entries = [
            entry for entry in layout.allocations() if entry.file_type == c.FILE_TYPE_DOTS
        ]
        assert len(entries) == 2
        for entry in entries:
            assert entry.rows_and_columns == (PICTURE_ROWS, PICTURE_COLUMNS)
            assert entry.schedule == c.DOTS_EIGHT_COLOUR

    def test_picture_labels_collide_with_neither_of_the_other_pools(self, layout):
        pictures = set(layout.pictures.labels)
        assert not pictures & set(c.TEXT_FILE_LABELS)
        assert not pictures & set(c.STRING_FILE_LABELS)
        assert c.FILE_PRIORITY not in pictures
        assert not pictures & set(c.RESERVED_FILE_LABELS)

    def test_every_icon_fits_the_file_it_would_be_written_into(self):
        # The geometry is fixed rather than configurable precisely because one
        # size fits all 148, and a picture wider than its file draws damaged.
        for icon in icons.ICONS.values():
            assert icon.width <= PICTURE_COLUMNS
            assert len(icon.rows) == PICTURE_ROWS

    def test_no_pictures_means_no_picture_files_are_allocated(self):
        layout = Layout(3, 256)
        assert not any(
            entry.file_type == c.FILE_TYPE_DOTS for entry in layout.allocations()
        )


class TestDrawing:
    async def test_calling_an_icon_writes_its_picture_then_the_message(
        self, registry, transport
    ):
        transport.clear()
        await add(registry, "door", "<icon:lock> LOCKED")

        # The picture before the message, so the sign is never drawing a file
        # that points at a picture not yet written.
        assert commands(transport)[:2] == [c.COMMAND_WRITE_DOTS, c.COMMAND_WRITE_TEXT]

    async def test_the_message_renders_to_the_call_and_the_picture_label(
        self, registry, transport
    ):
        await add(registry, "door", "<icon:lock> LOCKED")

        label = registry._state.pictures["lock"].label.encode("latin-1")
        text = [p for p in payloads(transport) if p[:1] == c.COMMAND_WRITE_TEXT][-1]
        assert c.DOTS_INSERT + label + b" LOCKED" in text

    async def test_the_picture_holds_the_icons_own_pixels(self, registry, transport):
        await add(registry, "door", "<icon:lock>")

        label = registry._state.pictures["lock"].label.encode("latin-1")
        assert picture_writes(transport)[-1] == frames.write_dots_file(
            label, icons.resolve("lock")
        )

    async def test_a_tint_gets_a_picture_of_its_own(self, registry, transport):
        # The tint changes the bitmap, so the two cannot share a file.
        await add(registry, "a", "<icon:check:green>")
        await add(registry, "b", "<icon:check:red>")

        assert set(registry._state.pictures) == {"check:green", "check:red"}
        labels = {picture.label for picture in registry._state.pictures.values()}
        assert len(labels) == 2

    async def test_two_messages_calling_one_icon_share_its_picture(
        self, registry, transport
    ):
        await add(registry, "a", "<icon:sun> HOT")
        transport.clear()
        await add(registry, "b", "<icon:sun> ALSO HOT")

        # The second message costs no picture write at all.
        assert picture_writes(transport) == []

    async def test_resending_the_same_message_writes_no_picture(self, registry, transport):
        await add(registry, "door", "<icon:lock> LOCKED")
        transport.clear()
        await add(registry, "door", "<icon:lock> LOCKED")

        assert picture_writes(transport) == []


class TestRefusals:
    async def test_an_icon_nobody_has_is_refused_in_the_librarys_words(self, registry):
        with pytest.raises(icons.UnknownIcon, match="no icon named 'teapot'"):
            await add(registry, "a", "<icon:teapot>")

    async def test_a_tint_on_a_fixed_colour_icon_is_refused(self, registry):
        with pytest.raises(icons.UntintableIcon, match="takes no tint"):
            await add(registry, "a", "<icon:sun:green>")

    async def test_an_unknown_tint_is_refused(self, registry):
        with pytest.raises(icons.UnknownTint, match="not a colour"):
            await add(registry, "a", "<icon:check:chartreuse>")

    async def test_a_refused_icon_leaves_no_slot_and_no_picture_behind(self, registry):
        with pytest.raises(icons.UnknownIcon):
            await add(registry, "a", "<icon:sun><icon:teapot>")

        assert registry._state.slots == {}
        # The sun was claimed before the teapot was reached, and giving it back
        # is what stops a rejected message from holding a file.
        assert registry._state.pictures == {}

    async def test_icons_are_refused_when_the_pool_is_switched_off(
        self, controller, store, state, clock
    ):
        registry = SlotRegistry(controller, Layout(3, 256), store, state, now=clock)
        await registry.restore()

        with pytest.raises(IconsDisabled, match="picture_count is 0"):
            await add(registry, "a", "<icon:sun>")

    async def test_a_message_too_long_gives_its_pictures_back(self, registry):
        from readerboard.services.registry import MessageTooLong

        with pytest.raises(MessageTooLong):
            await add(registry, "a", "<icon:sun>" + "X" * 400)

        assert registry._state.pictures == {}


class TestTheFullPool:
    async def test_a_third_icon_takes_the_file_of_one_nothing_calls(
        self, registry, transport
    ):
        await add(registry, "a", "<icon:sun>")
        await add(registry, "b", "<icon:moon>")
        # Nothing calls the sun now, so its file is the one going spare.
        await add(registry, "a", "PLAIN")

        await add(registry, "c", "<icon:star>")

        assert set(registry._state.pictures) == {"moon", "star"}

    async def test_a_third_icon_is_refused_when_both_files_are_called(self, registry):
        await add(registry, "a", "<icon:sun>")
        await add(registry, "b", "<icon:moon>")

        with pytest.raises(PicturePoolFull, match="raise picture_count"):
            await add(registry, "c", "<icon:star>")

    async def test_a_hidden_slot_holds_its_icon(self, registry):
        # Showing a hidden slot again is one run sequence write, and that is
        # only true while its icons still have their files.
        await add(registry, "a", "<icon:sun>")
        await add(registry, "b", "<icon:moon>")
        await registry.set_active("a", False)

        with pytest.raises(PicturePoolFull):
            await add(registry, "c", "<icon:star>")

    async def test_the_alert_holds_its_icon(self, registry, alerts):
        alerts.set_rendering(registry.rendering)
        await add(registry, "a", "<icon:sun>")
        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        with pytest.raises(PicturePoolFull):
            await add(registry, "c", "<icon:star>")

    async def test_a_slot_can_swap_its_icon_with_every_file_spoken_for(
        self, controller, store, state, clock
    ):
        # The message going out is not asked whether it still calls its icon,
        # because it is the one going out. With one file and one slot, moving
        # from sun to moon is a state that fits, so it has to be allowed.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        await add(registry, "weather", "<icon:sun>")

        await add(registry, "weather", "<icon:moon>")

        assert set(state.pictures) == {"moon"}

    async def test_the_alert_can_swap_its_icon_the_same_way(
        self, controller, store, state, clock, alerts
    ):
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts.set_rendering(registry.rendering)
        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        await alerts.raise_alert("<icon:sun> FINE", mode="HOLD")

        assert set(state.pictures) == {"sun"}

    async def test_one_message_wanting_more_icons_than_the_pool_says_the_pool_is_full(
        self, registry
    ):
        # Not "there is no picture holding <icon:sun>", which is what came back
        # while the third claim could evict the first one made for the same
        # message. A message cannot take a file away from itself.
        with pytest.raises(PicturePoolFull, match="raise picture_count"):
            await add(registry, "a", "<icon:sun><icon:moon><icon:star>")

    async def test_a_failed_write_gives_back_the_file_it_evicted(
        self, controller, store, state, clock, transport
    ):
        # The eviction is paid for by an icon that was not going anywhere, so a
        # write that then fails must not leave it with no file: nothing rewrites
        # a message whose text has not changed, and it would draw nothing for
        # good.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        await add(registry, "weather", "<icon:sun>")
        # Nothing calls the sun now, and its file is kept anyway, which is the
        # lazy release working. That makes it the file the next icon evicts.
        await add(registry, "weather", "PLAIN")
        label = state.pictures["sun"].label
        transport.fail_with = "cable unplugged"

        with pytest.raises(TransportError):
            await add(registry, "other", "<icon:moon>")

        assert set(state.pictures) == {"sun"}
        assert state.pictures["sun"].label == label

    async def test_an_alert_refused_for_its_length_draws_nothing_over_the_old_one(
        self, controller, store, state, clock, transport, alerts
    ):
        # The alert on the sign is calling the sun's file. An alert that is
        # refused must not have drawn the moon into it on the way: the refusal
        # happens after the render, so writing first would leave the alert that
        # is still up showing an icon nobody asked for.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts.set_rendering(registry.rendering)
        await alerts.raise_alert("<icon:sun>", mode="HOLD")
        label = state.pictures["sun"].label
        transport.clear()

        with pytest.raises(AlertTooLong):
            await alerts.raise_alert("<icon:moon> " + "X" * 200, mode="HOLD")

        assert picture_writes(transport) == []
        assert state.pictures["sun"].label == label

    async def test_an_alert_refused_for_being_second_claims_no_picture(
        self, controller, store, state, clock, transport, alerts
    ):
        # fail_if_active is checked inside the rendering, so the refusal takes
        # back whatever the new alert's icons were given on the way in. With one
        # picture file and the sun holding it, a moon that is refused must not
        # leave the pool full or the sun's file handed away.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts.set_rendering(registry.rendering)
        await alerts.raise_alert("<icon:sun>", mode="HOLD")
        label = state.pictures["sun"].label
        transport.clear()

        with pytest.raises(AlertAlreadyActive):
            await alerts.raise_alert("<icon:moon>", mode="HOLD", fail_if_active=True)

        assert picture_writes(transport) == []
        assert list(state.pictures) == ["sun"]
        assert state.pictures["sun"].label == label

    async def test_an_alert_that_fits_does_draw_its_icon(
        self, controller, store, state, clock, transport, alerts
    ):
        # The other half of the same change: deferring the write must not lose
        # it. The picture goes out before the priority file that calls it.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts.set_rendering(registry.rendering)
        transport.clear()

        await alerts.raise_alert("<icon:moon> DING", mode="HOLD")

        # Before the takeover, which is the write that matters. The release
        # ahead of it is not a message and is what gives the picture the sign to
        # itself; see TestWritingPicturesUnderAnAlert.
        sent = payloads(transport)
        picture = next(i for i, p in enumerate(sent) if p[:1] == c.COMMAND_WRITE_DOTS)
        takeover = next(
            i
            for i, p in enumerate(sent)
            if p[:2] == c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY and not is_release(p)
        )
        assert picture < takeover
        assert set(state.pictures) == {"moon"}

    async def test_a_save_that_fails_leaves_the_live_alert_its_picture(
        self, controller, store, state, clock, alerts, monkeypatch
    ):
        # The alert is on the sign by the time the state is written, so a save
        # that raises must not be read as "the alert did not land". It used to
        # be: the failure escaped the rendering, which gave the pictures back,
        # and the next icon to want a file took the one the alert was drawing.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        await registry.restore()
        alerts.set_rendering(registry.rendering)

        def explode(_state):
            raise OSError("read-only file system")

        monkeypatch.setattr(store, "save", explode)

        with pytest.raises(OSError):
            await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        assert state.alert is not None
        assert "bell" in state.pictures

    async def test_a_released_alerts_icon_can_be_taken(self, registry, alerts):
        alerts.set_rendering(registry.rendering)
        await add(registry, "a", "<icon:sun>")
        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")
        await alerts.release()

        await add(registry, "c", "<icon:star>")
        assert set(registry._state.pictures) == {"sun", "star"}

    async def test_a_file_is_kept_after_its_last_caller_goes(self, registry, transport):
        # Lazy release is the whole point: a source alternating between two
        # icons pays nothing, because neither file is ever given up.
        await add(registry, "a", "<icon:sun>")
        await add(registry, "a", "<icon:moon>")
        transport.clear()

        await add(registry, "a", "<icon:sun>")

        assert picture_writes(transport) == []


class TestAlerts:
    async def test_an_alert_can_carry_an_icon(self, registry, alerts, transport):
        alerts.set_rendering(registry.rendering)
        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        label = registry._state.pictures["bell"].label.encode("latin-1")
        priority = [
            p
            for p in payloads(transport)
            if p[:1] == c.COMMAND_WRITE_TEXT and p[1:2] == c.FILE_PRIORITY
        ][-1]
        assert c.DOTS_INSERT + label in priority

    async def test_the_picture_is_written_before_the_priority_file(
        self, registry, alerts, transport
    ):
        alerts.set_rendering(registry.rendering)
        transport.clear()
        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        # The bare release comes first, so that the sign takes the picture at
        # all. What this pins is that the picture is in the file before the
        # alert calling it goes up.
        sent = payloads(transport)
        assert is_release(sent[0])
        assert sent[1][:1] == c.COMMAND_WRITE_DOTS
        assert not is_release(sent[-1])
        assert sent[-1][:2] == c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY

    async def test_an_alert_naming_an_icon_nobody_has_is_refused(self, registry, alerts):
        alerts.set_rendering(registry.rendering)
        with pytest.raises(icons.UnknownIcon):
            await alerts.raise_alert("<icon:teapot>", mode="HOLD")

        assert registry._state.pictures == {}

    async def test_re_asserting_an_alert_claims_nothing(self, registry, alerts, transport):
        # It cannot: an icon it names already has its file, because the alert
        # calling it is what keeps the file. Claiming is a thing that can fail,
        # and failing to put the sign back is the worse outcome.
        alerts.set_rendering(registry.rendering)
        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")
        before = dict(registry._state.pictures)
        transport.clear()

        await alerts.reassert()

        assert registry._state.pictures == before
        assert picture_writes(transport) == []


class TestWritingPicturesUnderAnAlert:
    """The sign will not take a picture while a priority message is running.

    Measured on 2026-09-13, from a real sign. A slot with icons was covered by
    an alert, the service was restarted, and the alert was released: the icons
    were gone, or drawn as a sliver, and they stayed that way until the periodic
    refresh wrote them again a quarter of an hour later. Every picture write on
    that restart had landed under the alert the sign kept across it.

    Nothing retries such a write on its own. The controller remembers the bytes
    it put in each file and declines to send them twice, so a write the sign
    threw away is one the service believes it made. That is what turned a
    momentary failure into a display that stayed wrong.

    So a picture write is given the sign to itself: the alert comes off for it
    and goes straight back on. Everything below is either that happening when it
    should or, just as important, not happening when it should not.
    """

    async def test_the_bug_report_a_restart_during_an_alert(
        self, controller, layout, store, state, clock, transport
    ):
        # The whole failure, end to end, in the order it happened. A slot with
        # an icon, an alert holding the sign, and a restart: what the sign gets
        # on the way back up has to hand it back before the picture and take it
        # again after, or the icon is written into a file the sign is ignoring.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)
        await add(registry, "door", "<icon:lock> LOCKED")
        await alerts.raise_alert("FIRE", mode="HOLD")

        # A restart: new controller, so nothing is remembered about the sign,
        # and the sign is still holding the alert from before it.
        fresh = SignController(transport, inter_packet_delay=0)
        transport.clear()
        again = SlotRegistry(fresh, layout, store, state, now=clock)
        alerts_again = AlertService(fresh, store, state, now=clock)
        alerts_again.set_rendering(again.rendering)
        again.set_priority_hold(alerts_again.lifted)

        await again.restore()
        after_restore = list(payloads(transport))
        await alerts_again.restore()

        priority = priority_writes(transport)
        assert is_release(priority[0]), "the sign was not handed back for the picture"
        assert len(picture_writes(transport)) == 1
        # The release comes before the picture and the alert comes back after.
        assert after_restore.index(priority[0]) < after_restore.index(
            picture_writes(transport)[0]
        )
        assert not is_release(priority[1])
        # And the alert service's own restore adds nothing: the put-back already
        # sent those bytes, so the write it makes is suppressed rather than
        # taking the sign off the alert a second time.
        assert payloads(transport)[len(after_restore) :] == []

    async def test_a_restart_hands_the_sign_back_before_the_pictures(
        self, controller, layout, store, state, clock, transport
    ):
        # The unrecorded takeover. An unclean stop between writing an alert and
        # saving it leaves the sign holding one with nothing in the state file
        # to say so, which the alert service already takes seriously: its
        # restore clears the priority file even with no alert recorded. But that
        # runs after the registry's restore, so every picture on the way back up
        # went out underneath a message the sign was still showing.
        #
        # So the release has to come first, and it does not wait to be told an
        # alert is up.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)
        await add(registry, "door", "<icon:lock> LOCKED")

        fresh = SignController(transport, inter_packet_delay=0)
        transport.clear()
        again = SlotRegistry(fresh, layout, store, state, now=clock)
        alerts_again = AlertService(fresh, store, state, now=clock)
        alerts_again.set_rendering(again.rendering)
        again.set_priority_hold(alerts_again.lifted)

        assert state.alert is None, "the point is that nothing is recorded"
        await again.restore()

        sent = payloads(transport)
        release = next(i for i, p in enumerate(sent) if is_release(p))
        picture = next(i for i, p in enumerate(sent) if p[:1] == c.COMMAND_WRITE_DOTS)
        assert release < picture, "the picture went out under whatever the sign held"

    async def test_a_dropped_picture_record_is_repaired_by_a_refresh(
        self, controller, store, state, clock, transport
    ):
        # The rollback gives up a file whose bitmap it could not put back, so
        # that nothing renders a call to a file holding the wrong picture. That
        # leaves the icon with no file, and the message calling it keeps calling
        # it, so something has to give it one again. Nothing rewrites a message
        # that has not changed, which is what _reclaim_pictures is for, and the
        # refresh is where it gets its chance.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)

        await alerts.raise_alert("<icon:sun> FINE", mode="HOLD")
        assert "sun" in state.pictures

        real_dots = controller.write_dots_file
        drawn: list[int] = []

        async def refuse_the_redraw(*args, **kwargs):
            drawn.append(1)
            if len(drawn) > 1:
                raise TransportError("refused the redraw")
            return await real_dots(*args, **kwargs)

        real_priority = controller.write_priority
        priority_calls: list[int] = []

        async def fail_only_the_new_alert(*args, **kwargs):
            priority_calls.append(1)
            if len(priority_calls) == 1:
                raise TransportError("the new alert never landed")
            return await real_priority(*args, **kwargs)

        controller.write_dots_file = refuse_the_redraw  # type: ignore[method-assign]
        controller.write_priority = fail_only_the_new_alert  # type: ignore[method-assign]
        try:
            with pytest.raises(TransportError):
                await alerts.raise_alert("<icon:moon> RAIN", mode="HOLD")
        finally:
            controller.write_dots_file = real_dots  # type: ignore[method-assign]
            controller.write_priority = real_priority  # type: ignore[method-assign]

        assert "sun" not in state.pictures, "the setup did not reach the drop"

        await registry.refresh()

        assert "sun" in state.pictures
        assert registry.in_sync

    async def test_a_slot_with_a_new_icon_written_under_an_alert(
        self, wired, transport
    ):
        registry, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        await add(registry, "door", "<icon:lock> LOCKED")

        order = [payload[:2] for payload in payloads(transport)]
        release = c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY
        assert order[0] == release
        assert order[1][:1] == c.COMMAND_WRITE_DOTS
        # Back on the sign before the slot's own file is written, so the alert
        # is off the display for one picture write and nothing else.
        assert order[2] == release
        assert not is_release(priority_writes(transport)[-1])

    async def test_the_sign_is_handed_back_even_with_no_alert_recorded(
        self, wired, transport
    ):
        # What this service has written down is not a reading of the sign. An
        # unclean stop between writing an alert and saving it leaves a takeover
        # nothing here knows about, which is why the alert service clears the
        # priority file at startup with no alert recorded at all. That clear
        # happens after the registry has restored, so a picture written on the
        # way back up would still be lost under it.
        #
        # So the release does not wait on the record. It is a release and never
        # a takeover: nothing here puts a message on that file.
        registry, _ = wired
        transport.clear()

        await add(registry, "door", "<icon:lock> LOCKED")

        assert picture_writes(transport)
        assert [p for p in priority_writes(transport) if not is_release(p)] == []
        assert len(priority_writes(transport)) == 1

    async def test_handing_the_sign_back_costs_one_write_and_not_one_per_picture(
        self, wired, transport
    ):
        # The release is unforced, so the controller sends it once and suppresses
        # every repeat. Without that this would be an extra packet on every icon
        # any source ever writes.
        registry, _ = wired
        await add(registry, "door", "<icon:lock> LOCKED")
        transport.clear()

        await add(registry, "gate", "<icon:bell> RING")

        assert picture_writes(transport), "no picture, so this proves nothing"
        assert priority_writes(transport) == []

    async def test_an_icon_that_already_has_its_file_lifts_nothing(
        self, wired, transport
    ):
        # The common case, and the one that must stay free. A message calling an
        # icon that is already in the pool claims nothing and writes no picture,
        # so there is nothing to hand the sign back for.
        registry, alerts = wired
        await add(registry, "door", "<icon:lock> LOCKED")
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        await add(registry, "gate", "<icon:lock> ALSO LOCKED")

        assert picture_writes(transport) == []
        assert priority_writes(transport) == []

    async def test_an_alert_replacing_one_with_a_new_icon_hands_the_sign_back(
        self, wired, transport
    ):
        # The alert service's own case. The picture for the new alert is written
        # while the old alert is still on the priority file, so it needs the
        # same treatment, and it is the alert service that does it rather than
        # the registry: the registry is called from inside this lock.
        _, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        order = payloads(transport)
        assert is_release(order[0])
        assert order[1][:1] == c.COMMAND_WRITE_DOTS
        assert not is_release(order[2])

    async def test_an_alert_replacing_one_with_no_new_icon_does_not(
        self, wired, transport
    ):
        # Gated on there being a picture. Without that every alert replacing an
        # alert would hand the sign back and take it again for nothing.
        _, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        await alerts.raise_alert("FLOOD", mode="HOLD")

        assert picture_writes(transport) == []
        assert [p for p in priority_writes(transport) if is_release(p)] == []

    async def test_a_refresh_under_an_alert_hands_the_sign_back(self, wired, transport):
        registry, alerts = wired
        await add(registry, "door", "<icon:lock> LOCKED")
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        await registry.refresh()

        # A refresh forgets what the sign holds and writes everything again, so
        # the picture really does go out, and it must not go out under the alert.
        assert len(picture_writes(transport)) == 1
        assert is_release(priority_writes(transport)[0])
        assert not is_release(priority_writes(transport)[-1])

    async def test_a_picture_write_that_fails_still_puts_the_alert_back(
        self, wired, transport, monkeypatch
    ):
        # The one that matters most for the sign in the room. A write that
        # failed under a lifted alert must not leave the rotation showing with
        # an alert recorded as holding the display and nothing to put it back
        # until the refresh a quarter of an hour later.
        registry, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        real = registry._controller.write_dots_file

        async def fail(*args, **kwargs):
            raise TransportError("the sign went away mid-picture")

        monkeypatch.setattr(registry._controller, "write_dots_file", fail)
        with pytest.raises(TransportError):
            await add(registry, "door", "<icon:lock> LOCKED")
        monkeypatch.setattr(registry._controller, "write_dots_file", real)

        assert alerts.active is not None
        assert not is_release(priority_writes(transport)[-1])

    async def test_an_alert_replacing_one_puts_it_back_when_its_picture_fails(
        self, wired, transport, monkeypatch
    ):
        # The alert service's own rollback, and the mirror of the registry's.
        # Handing the sign back for a picture that then fails must not leave the
        # rotation showing with an alert recorded as holding the display: before
        # the hand-back existed, a failure here left the old alert up, because
        # nothing had touched the priority file.
        _, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        async def fail(*args, **kwargs):
            raise TransportError("the sign went away mid-picture")

        monkeypatch.setattr(alerts._controller, "write_dots_file", fail)
        with pytest.raises(TransportError):
            await alerts.raise_alert("<icon:bell> DING", mode="HOLD")

        assert alerts.active is not None
        assert alerts.active.message == "FIRE"
        assert not is_release(priority_writes(transport)[-1])

    async def test_a_refused_alert_gives_back_the_icon_it_evicted(
        self, controller, store, state, clock, transport
    ):
        # The pool is full and the alert being replaced is the only caller of
        # the icon in it, so claiming the new alert's icon evicts it. If the new
        # alert then fails to land, the old one is put back on the display, and
        # it has to come back whole: calling its own file, with its own bitmap
        # in it.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)

        await alerts.raise_alert("<icon:sun> FINE", mode="HOLD")
        sun = state.pictures["sun"].label
        transport.clear()

        real = controller.write_priority
        calls: list[int] = []

        async def fail_the_new_alerts_own_write(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise TransportError("the new alert never landed")
            return await real(*args, **kwargs)

        controller.write_priority = fail_the_new_alerts_own_write  # type: ignore[method-assign]
        try:
            with pytest.raises(TransportError):
                await alerts.raise_alert("<icon:moon> RAIN", mode="HOLD")
        finally:
            controller.write_priority = real  # type: ignore[method-assign]

        # The record is back, in the file it was in.
        assert list(state.pictures) == ["sun"]
        assert state.pictures["sun"].label == sun
        # The sun was drawn again, after the moon took the file, so the file
        # holds what the alert going back up is about to call.
        assert len(picture_writes(transport)) == 2
        # And the alert that was up is on the priority file calling it.
        last = priority_writes(transport)[-1]
        assert not is_release(last)
        assert c.DOTS_INSERT + sun.encode("latin-1") in last
        assert alerts.active is not None
        assert alerts.active.message == "<icon:sun> FINE"

    async def test_a_refused_alert_never_leaves_the_old_one_calling_a_wrong_bitmap(
        self, controller, store, state, clock, transport
    ):
        # The property, rather than one path to it. After a refused alert there
        # are two states worth having and one worth refusing. Either the old
        # alert is back calling its file and that file was redrawn for it, or it
        # is not calling a picture at all and the next refresh repairs it. What
        # must never happen is the old alert calling a file that another icon's
        # bitmap is sitting in, which puts a wrong picture on the display rather
        # than a missing one.
        #
        # So this is the awkward half-failure that produces it: the redraw is
        # refused and the priority write is not. Everything succeeding is the
        # test above; everything failing writes nothing and proves nothing.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 1), store, state, now=clock)
        await registry.restore()
        alerts = AlertService(controller, store, state, now=clock)
        alerts.set_rendering(registry.rendering)
        registry.set_priority_hold(alerts.lifted)

        await alerts.raise_alert("<icon:sun> FINE", mode="HOLD")
        sun = state.pictures["sun"].label
        transport.clear()

        real_dots = controller.write_dots_file
        drawn: list[int] = []

        async def refuse_the_redraw(*args, **kwargs):
            drawn.append(1)
            # The moon goes out, which is what takes the file off the sun. The
            # redraw that would give it back does not.
            if len(drawn) > 1:
                raise TransportError("refused the redraw")
            return await real_dots(*args, **kwargs)

        real_priority = controller.write_priority
        priority_calls: list[int] = []

        async def fail_only_the_new_alert(*args, **kwargs):
            priority_calls.append(1)
            if len(priority_calls) == 1:
                raise TransportError("the new alert never landed")
            return await real_priority(*args, **kwargs)

        controller.write_dots_file = refuse_the_redraw  # type: ignore[method-assign]
        controller.write_priority = fail_only_the_new_alert  # type: ignore[method-assign]
        try:
            with pytest.raises(TransportError):
                await alerts.raise_alert("<icon:moon> RAIN", mode="HOLD")
        finally:
            controller.write_dots_file = real_dots  # type: ignore[method-assign]
            controller.write_priority = real_priority  # type: ignore[method-assign]

        # The put-back did happen, so this is the dangerous shape and not a
        # vacuous pass.
        takeovers = [p for p in priority_writes(transport) if not is_release(p)]
        assert takeovers, "nothing was put back, so this proves nothing"
        assert c.DOTS_INSERT + sun.encode("latin-1") not in takeovers[-1], (
            "the old alert was put back calling a file the moon is still drawn in"
        )
        # And the service stopped claiming a file it could not fill.
        assert "sun" not in state.pictures

    async def test_a_put_back_that_fails_on_its_own_is_reported(
        self, wired, transport, monkeypatch
    ):
        # The picture went out and the write the caller asked for worked, so
        # nothing is in flight to protect and the sign is the thing left wrong:
        # the rotation is showing while the service still reports an alert
        # holding the display. Swallowing this answered the call with a success
        # over exactly that, and nothing said so until the periodic re-assert a
        # quarter of an hour later.
        registry, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        async def fail(*args, **kwargs):
            raise TransportError("the sign went away after the picture")

        # Only the put-back. clear_priority builds its own frame and does not go
        # through this, so the hand-back still happens and the picture is still
        # written; it is putting the alert back that fails.
        monkeypatch.setattr(alerts._controller, "write_priority", fail)

        with pytest.raises(TransportError, match="after the picture"):
            await add(registry, "door", "<icon:lock> LOCKED")

    async def test_a_put_back_that_fails_does_not_replace_the_real_failure(
        self, wired, transport, monkeypatch, caplog
    ):
        # The put-back runs in a finally, so an exception from it would arrive
        # in place of whatever the picture write raised, and the caller would be
        # told the sign is unreachable when what actually happened was the pool
        # filling up. It is logged and swallowed instead; the periodic
        # re-assert is what puts the alert back after a failure like this.
        registry, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD")
        transport.clear()

        async def fail_dots(*args, **kwargs):
            raise TransportError("the picture write failed")

        async def fail_priority(*args, **kwargs):
            raise TransportError("and so did putting the alert back")

        monkeypatch.setattr(registry._controller, "write_dots_file", fail_dots)
        monkeypatch.setattr(registry._controller, "write_priority", fail_priority)

        with (
            caplog.at_level(logging.WARNING),
            pytest.raises(TransportError, match="the picture write failed"),
        ):
            await add(registry, "door", "<icon:lock> LOCKED")

        assert "could not put the lifted alert back" in caplog.text

    async def test_an_alert_that_expired_while_lifted_is_not_put_back(
        self, wired, transport, clock
    ):
        # Its deadline passed while the sign was being written to. Putting it
        # back would show it again for as long as it takes the sweep to come
        # round, which is an alert appearing after it was over.
        registry, alerts = wired
        await alerts.raise_alert("FIRE", mode="HOLD", ttl_seconds=30)
        transport.clear()
        clock.advance(31)

        await add(registry, "door", "<icon:lock> LOCKED")

        assert alerts.active is None
        assert is_release(priority_writes(transport)[0])
        assert [p for p in priority_writes(transport) if not is_release(p)] == []

    async def test_the_registry_writes_as_it_always_did_with_no_alert_service(
        self, registry, transport
    ):
        # Nothing wired in, which is most of this file and every test that uses
        # the registry on its own. The hook is optional so that those keep
        # working rather than needing an alert service they have no use for.
        transport.clear()
        await add(registry, "door", "<icon:lock> LOCKED")

        assert len(picture_writes(transport)) == 1
        assert priority_writes(transport) == []


class TestAcrossARestart:
    async def test_an_icon_comes_back_in_the_file_it_was_in(
        self, controller, layout, store, state, clock, transport
    ):
        # The pairing is the thing that has to survive. A message renders to its
        # picture's label as raw bytes, so an icon re-claimed into a different
        # file would leave the stored message pointing at the wrong one.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")
        label = state.pictures["lock"].label

        again = SlotRegistry(controller, layout, store, state, now=clock)
        await again.restore()

        assert state.pictures["lock"].label == label

    async def test_a_real_restart_does_rewrite_every_picture(
        self, controller, layout, store, state, clock, transport
    ):
        # Worth pinning because it is easy to assume otherwise. Keeping the
        # record does not save the write: a restart builds a new controller, and
        # what stops a picture being sent twice is that controller remembering
        # what it put in each file. A fresh one remembers nothing, so restoring
        # costs one picture write, and one blank, for every icon on the sign.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")

        fresh = SignController(transport, inter_packet_delay=0)
        transport.clear()
        again = SlotRegistry(fresh, layout, store, state, now=clock)
        await again.restore()

        assert len(picture_writes(transport)) == 1

    async def test_the_same_controller_writes_no_picture_twice(
        self, controller, layout, store, state, clock, transport
    ):
        # And the other half: suppression is what makes the ordinary refresh
        # free, since the controller already holds those exact bytes.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")
        transport.clear()

        again = SlotRegistry(controller, layout, store, state, now=clock)
        await again.restore()

        assert picture_writes(transport) == []

    async def test_a_picture_whose_label_left_the_pool_is_given_another(
        self, controller, layout, store, state, clock
    ):
        # A stored picture whose label is not in the pool. A changed label set
        # reallocates the sign, which erases the messages with it, so what is
        # left here is a record that disagrees with the pool for some other
        # reason. The picture is dropped and this puts it back, which is what
        # keeps the message calling it from drawing nothing for good.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")
        state.pictures["lock"].label = "~"

        again = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        await again.restore()

        assert state.pictures["lock"].label in [
            label.decode("latin-1") for label in c.PICTURE_FILE_LABELS[:2]
        ]
        assert "door" in state.slots

    async def test_pictures_are_written_before_the_messages_that_call_them(
        self, controller, layout, store, state, clock, transport
    ):
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")
        transport.clear()

        await registry.refresh()

        sent = commands(transport)
        assert sent.index(c.COMMAND_WRITE_DOTS) < sent.index(c.COMMAND_WRITE_TEXT)

    async def test_an_icon_a_later_version_dropped_says_so_and_still_starts(
        self, controller, layout, store, state, clock, caplog, monkeypatch
    ):
        # The library is data, and data changes between versions. A stored
        # message calling an icon this version no longer has must not be a start
        # that fails: the service is expected back after a power cut with nobody
        # at the machine.
        #
        # Nothing else is wrong here. The label is still a good one and the pool
        # is the same shape, which is the ordinary upgrade: the only thing that
        # changed is the library. That matters because every picture is written
        # before any message, so an icon that cannot be drawn would raise there
        # and take the whole restore with it.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "a", "<icon:sun>")
        monkeypatch.delitem(icons.ICONS, "sun")

        with caplog.at_level(logging.WARNING):
            again = SlotRegistry(controller, layout, store, state, now=clock)
            await again.restore()

        assert "which this version does not have" in caplog.text
        assert "sun" not in state.pictures
        # The slot keeps its place. The call draws nothing, which is what the
        # sign itself draws for a call to a picture that is not there.
        assert "a" in state.slots

    async def test_a_tint_a_later_version_dropped_goes_the_same_way(
        self, controller, layout, store, state, clock, monkeypatch
    ):
        # An icon that stops taking a tint is the other half of the same change,
        # and resolve refuses it rather than ignoring the tint, so it has to be
        # caught in the same place.
        registry = SlotRegistry(controller, layout, store, state, now=clock)
        await registry.restore()
        await add(registry, "a", "<icon:check:red>")
        monkeypatch.setitem(
            icons.ICONS, "check", replace(icons.ICONS["check"], tint=None)
        )

        again = SlotRegistry(controller, layout, store, state, now=clock)
        await again.restore()

        assert "check:red" not in state.pictures
        assert "a" in state.slots


class TestReconfiguring:
    def test_a_state_file_from_before_icons_still_matches(self):
        from readerboard.sign.state import AppliedLayout

        # The fields default to nothing, so an old file reads as a layout with
        # no pictures and does not cost an erase on upgrade.
        old = AppliedLayout(slot_count=3, slot_capacity=256, labels=["A", "B", "C"])
        assert not Layout(3, 256).needs_reconfiguration(old)

    def test_a_state_file_from_a_released_version_still_matches(self):
        from readerboard.sign.state import AppliedLayout

        # The shape 0.6.0 actually wrote: slots and variables with their labels,
        # and no picture fields at all. Upgrading to a version that has icons,
        # with picture_count left at its default of 0, must cost nothing.
        released = AppliedLayout.model_validate(
            {
                "slot_count": 8,
                "slot_capacity": 256,
                "labels": list("ABCDEFGH"),
                "variable_count": 8,
                "variable_capacity": 32,
                "variable_labels": list("abcdefgh"),
            }
        )
        assert not Layout(8, 256, 8, 32, 0).needs_reconfiguration(released)

    def test_a_changed_label_set_costs_a_reallocation(self):
        from readerboard.sign.state import AppliedLayout

        # The one nothing else catches. The count and the geometry are the same,
        # so no other check fires, but the sign holds the files the old labels
        # named while the pool hands out the new ones. Every picture write would
        # go to a file the sign never allocated, be discarded without a word,
        # and the icons would come back blank.
        layout = Layout(3, 256, 0, 32, 2)
        applied: AppliedLayout = layout.as_applied()
        moved = applied.model_copy(update={"picture_labels": ["!", "#"]})

        assert layout.needs_reconfiguration(moved)

    async def test_a_rotated_label_table_costs_the_service_nothing(
        self, controller, store, state, clock, monkeypatch, transport
    ):
        # End to end, and through the real path rather than a hand-built record:
        # the labels come from a table in the code, so this edits the table and
        # starts again. Checking that needs_reconfiguration says yes is not the
        # same as checking the service acts on it, and acting on it is what puts
        # the sign's files and the pool back in agreement.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")
        assert state.pictures

        rotated = (
            c.PICTURE_FILE_LABELS[1],
            c.PICTURE_FILE_LABELS[0],
            *c.PICTURE_FILE_LABELS[2:],
        )
        monkeypatch.setattr(c, "PICTURE_FILE_LABELS", rotated)
        transport.clear()

        again = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        await again.restore()

        # A rotation of the first two is the same set, so nothing is reallocated
        # and the messages survive.
        assert frames.packet(frames.clear_memory()) not in transport.packets
        assert "door" in state.slots

    async def test_the_service_reallocates_when_the_labels_themselves_change(
        self, controller, store, state, clock, monkeypatch, transport
    ):
        # And when the set really does change, the sign is reallocated, which
        # erases it. That is the expensive answer and the only correct one: the
        # sign holds the files the old table named, so every write to a new label
        # would land in a file it never allocated and be discarded in silence.
        registry = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        await registry.restore()
        await add(registry, "door", "<icon:lock> LOCKED")
        transport.clear()

        different = (b"{", b"|", *c.PICTURE_FILE_LABELS[2:])
        monkeypatch.setattr(c, "PICTURE_FILE_LABELS", different)

        again = SlotRegistry(controller, Layout(3, 256, 0, 32, 2), store, state, now=clock)
        await again.restore()

        assert frames.packet(frames.clear_memory()) in transport.packets
        assert state.slots == {}
        assert state.pictures == {}

    def test_the_same_labels_in_a_different_order_cost_nothing(self):
        from readerboard.sign.state import AppliedLayout

        # Every label is still one the sign has, and which key gets which file
        # is decided at run time rather than read from the record, so there is
        # nothing to repair and an erase would be paid for nothing.
        layout = Layout(3, 256, 0, 32, 2)
        applied: AppliedLayout = layout.as_applied()
        shuffled = applied.model_copy(
            update={"picture_labels": list(reversed(applied.picture_labels))}
        )

        assert not layout.needs_reconfiguration(shuffled)

    def test_turning_icons_on_costs_a_reallocation(self):
        applied = Layout(3, 256).as_applied()
        assert Layout(3, 256, 0, 32, 2).needs_reconfiguration(applied)

    def test_changing_how_many_pictures_costs_a_reallocation(self):
        applied = Layout(3, 256, 0, 32, 2).as_applied()
        assert Layout(3, 256, 0, 32, 4).needs_reconfiguration(applied)

    def test_the_same_pool_twice_costs_nothing(self):
        applied = Layout(3, 256, 0, 32, 2).as_applied()
        assert not Layout(3, 256, 0, 32, 2).needs_reconfiguration(applied)

    def test_a_change_to_the_picture_geometry_costs_a_reallocation(self):
        # Not a setting, so nothing in a configuration file can cause this. It
        # is carried so that widening an icon in the code is noticed rather than
        # leaving the sign holding files of the old size.
        applied = Layout(3, 256, 0, 32, 2).as_applied()
        applied.picture_columns += 1
        assert Layout(3, 256, 0, 32, 2).needs_reconfiguration(applied)
