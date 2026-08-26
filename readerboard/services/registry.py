"""The set of messages currently sharing the sign.

A slot is a named place on the sign that a source owns. Home Assistant owns
``temperature``, a doorbell automation might own ``doorbell``, and each one
writes to its own slot without knowing or caring about the others. Writing to a
slot that already exists replaces it rather than adding another, so a source
that re-sends every five minutes does not accumulate anything.

Each slot lives in its own sign file, and the run sequence names the occupied
files in order. That is the whole rotation mechanism: the sign cycles them by
itself, so a slot appearing or disappearing costs one small write and nothing
after that.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from readerboard.protocol.markup import render
from readerboard.protocol.tokens import MODE_BY_NAME, POSITION_BY_NAME
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout, LayoutFull
from readerboard.sign.state import ServiceState, SlotState, StateStore
from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)

# The slot the simple write endpoint uses, for callers that do not name one of
# their own. It has no TTL by default, so what it holds stays on the sign until
# something replaces it.
DEFAULT_SLOT_KEY = "default"


class RegistryError(Exception):
    """Something was wrong with a request to change the registry."""


class UnknownSlot(RegistryError):
    """No slot by that name is registered."""


class MessageTooLong(RegistryError):
    """The rendered message does not fit the sign file allocated to it."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MessageRegistry:
    """Owns the slots, the file pool, and the run sequence."""

    def __init__(
        self,
        controller: SignController,
        layout: Layout,
        store: StateStore,
        state: ServiceState,
        *,
        now: Callable[[], datetime] = _utcnow,
        alert_active: Callable[[], bool] = lambda: False,
    ) -> None:
        """Wire the registry to the sign and to the state it was restored from.

        ``alert_active`` lets the registry know when an alert is holding the
        display. See :meth:`_apply_run_sequence` for why that matters.
        """
        self._controller = controller
        self._layout = layout
        self._store = store
        self._state = state
        self._now = now
        self._alert_active = alert_active
        self._lock = asyncio.Lock()
        self._deferred_run_sequence: list[bytes] | None = None
        self._dirty = False

    # == reading ============================================================

    def list_slots(self) -> list[SlotState]:
        """Every registered slot, in the order the sign plays them."""
        return sorted(self._state.slots.values(), key=lambda slot: (slot.order, slot.key))

    def get(self, key: str) -> SlotState:
        """One slot by name."""
        slot = self._state.slots.get(key)
        if slot is None:
            raise UnknownSlot("no slot named %r is registered" % key)
        return slot

    @property
    def occupancy(self) -> tuple[int, int]:
        """How many slots are used, and how many there are in total."""
        return len(self._state.slots), self._layout.slot_count

    # == startup ============================================================

    async def restore(self) -> None:
        """Bring the sign back to what the state file says it should be showing.

        Reconfiguring memory is avoided unless the pool itself changed, because
        it erases the sign. On the ordinary restart, where nothing changed, this
        rewrites the files and the run sequence and the sign never blinks.
        """
        async with self._lock:
            if self._layout.needs_reconfiguration(self._state.layout):
                logger.info("the sign's file pool has changed, so it must be reallocated")
                await self._controller.apply_memory_config(self._layout.allocations())
                self._state.layout = self._layout.as_applied()
                # Every file was just erased, so nothing survives from before.
                self._state.slots = {}

            self._reattach_labels()
            # force, because the state file may say an alert was active but
            # nothing has been re-asserted on the sign yet.
            await self._rewrite_all(force=True)
            self._save()

    def _reattach_labels(self) -> None:
        """Re-establish which slot owns which file, dropping any that no longer fit."""
        for key, slot in list(self._state.slots.items()):
            try:
                self._layout.restore(key, slot.label.encode("ascii"))
            except ValueError:
                logger.warning(
                    "slot %r used file %s, which is outside the current pool; dropping it",
                    key,
                    slot.label,
                )
                del self._state.slots[key]

    async def _rewrite_all(self, *, force: bool = False) -> None:
        for slot in self.list_slots():
            await self._write_slot(slot)
        await self._apply_run_sequence(force=force)

    async def refresh(self) -> None:
        """Push everything to the sign again, whether or not it looks necessary.

        This is the answer to a problem the suppression cache cannot see. The
        sign and the Ethernet adapter are separately powered, so the sign can be
        power cycled while the TCP link to the adapter stays up. No reconnect
        fires, the cache stays warm, and suppression then skips exactly the
        writes that would repair a sign which is now blank.

        Refreshing drops what the controller believes about the sign's contents
        and writes it all again. It runs on a timer, and on every reconnect.

        If the Phase 0 spike shows the sign answers read commands through the
        adapter, this can become a read-back comparison that only writes on a
        real mismatch. The frame builders for those reads already exist; what is
        unproven is whether two-way traffic works over that path at all.
        """
        async with self._lock:
            self._controller.forget_sign_contents()
            await self._rewrite_all()
            self._dirty = False

        logger.debug("re-pushed %d slot(s) to the sign", len(self._state.slots))

    @property
    def in_sync(self) -> bool:
        """Whether everything registered is believed to be on the sign."""
        return not self._dirty

    # == changing ===========================================================

    async def upsert(
        self,
        key: str,
        message: str,
        *,
        mode: str,
        position: str,
        order: int = 0,
        ttl_seconds: float | None = None,
        source: str | None = None,
        strict: bool = True,
    ) -> SlotState:
        """Register or replace a slot and put it on the sign."""
        body = render(message, strict=strict)
        if len(body) > self._layout.slot_capacity:
            raise MessageTooLong(
                "the message renders to %d bytes but each slot holds %d. Shorten it, or "
                "raise slot_capacity and restart, which reallocates the sign and clears it."
                % (len(body), self._layout.slot_capacity)
            )

        mode_token = MODE_BY_NAME[mode]
        position_token = POSITION_BY_NAME[position]

        async with self._lock:
            existed = key in self._state.slots
            label = self._layout.assign(key)  # raises LayoutFull when the pool is full

            now = self._now()
            slot = SlotState(
                key=key,
                label=label.decode("ascii"),
                message=message,
                mode=mode,
                position=position,
                order=order,
                source=source,
                expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
                updated_at=now,
            )

            self._state.slots[key] = slot
            try:
                await self._controller.write_text_file(
                    label, body, mode=mode_token.value, position=position_token.value
                )
                if not existed:
                    await self._apply_run_sequence()
            except TransportError as err:
                # The registry is the durable record of what should be on the
                # sign, so a request that validated is a request we can satisfy
                # even with the sign unplugged. Keep it, and converge when the
                # link is back, rather than making Home Assistant hold the retry
                # logic this service exists to take off it. /health says the
                # sign is out of sync in the meantime.
                self._dirty = True
                logger.warning(
                    "slot %r accepted but the sign is unreachable (%s); it will be "
                    "written when the link is back",
                    key,
                    err,
                )
            except Exception:
                # Something other than the link is wrong, so this slot is not
                # something we can promise to deliver. Give its file back rather
                # than leaking one per failed request.
                del self._state.slots[key]
                if not existed:
                    self._layout.release(key)
                raise

            self._save()

        logger.info(
            "slot %r %s in file %s%s",
            key,
            "updated" if existed else "registered",
            slot.label,
            " from %s" % source if source else "",
        )
        return slot

    async def remove(self, key: str) -> None:
        """Take a slot off the sign and forget it."""
        async with self._lock:
            slot = self._state.slots.pop(key, None)
            if slot is None:
                raise UnknownSlot("no slot named %r is registered" % key)

            self._layout.release(key)
            try:
                await self._apply_run_sequence()
                await self._blank(slot)
            except TransportError as err:
                self._dirty = True
                logger.warning("slot %r removed but the sign is unreachable (%s)", key, err)
            self._save()

        logger.info("slot %r removed, freeing file %s", key, slot.label)

    async def clear(self) -> int:
        """Take every slot off the sign. Returns how many there were."""
        async with self._lock:
            slots = list(self._state.slots.values())
            self._state.slots.clear()
            for slot in slots:
                self._layout.release(slot.key)

            try:
                await self._apply_run_sequence()
                for slot in slots:
                    await self._blank(slot)
            except TransportError as err:
                self._dirty = True
                logger.warning("slots cleared but the sign is unreachable (%s)", err)
            self._save()

        return len(slots)

    async def sweep(self) -> list[str]:
        """Drop slots whose TTL has passed. Returns the keys that went."""
        now = self._now()
        async with self._lock:
            expired = [
                slot
                for slot in self._state.slots.values()
                if slot.expires_at is not None and slot.expires_at <= now
            ]
            if not expired:
                return []

            for slot in expired:
                del self._state.slots[slot.key]
                self._layout.release(slot.key)

            # Taking the labels out of the run sequence is what removes them
            # from the sign. The files themselves are left alone, because
            # reallocating to reclaim them would erase everything else.
            try:
                await self._apply_run_sequence()
                for slot in expired:
                    await self._blank(slot)
            except TransportError as err:
                self._dirty = True
                logger.warning("slots expired but the sign is unreachable (%s)", err)
            self._save()

        keys = [slot.key for slot in expired]
        logger.info("slot(s) %s expired and left the rotation", ", ".join(keys))
        return keys

    # == internals ==========================================================

    async def _write_slot(self, slot: SlotState) -> None:
        # Content already accepted once is re-rendered leniently, so that a slot
        # restored from disk cannot fail to come back because the rules around
        # it tightened in the meantime.
        body = render(slot.message, strict=False)
        await self._controller.write_text_file(
            slot.label.encode("ascii"),
            body,
            mode=MODE_BY_NAME[slot.mode].value,
            position=POSITION_BY_NAME[slot.position].value,
        )

    async def _blank(self, slot: SlotState) -> None:
        """Empty a file that no longer holds a slot.

        The run sequence has already stopped naming it, so this is tidiness
        rather than necessity. It matters when the file is handed to a different
        slot later, since a stale body would otherwise be what the suppression
        cache compares against.
        """
        await self._controller.write_text_file(slot.label.encode("ascii"), b"")

    async def _apply_run_sequence(self, *, force: bool = False) -> None:
        """Tell the sign which files to play, unless an alert is holding it.

        The protocol says a running priority message is cancelled by a serial
        write to the run time table or the run day table, and says nothing
        either way about a write to the run sequence. Until the spike settles
        that on real hardware, the safe reading is that it might: a slot
        expiring during an alert would otherwise take the alert off the display
        with nothing to explain why.

        So while an alert is up, the sequence is remembered and applied when the
        sign is handed back. Writing a slot's own TEXT file is not on the
        protocol's list and carries on as normal, so content stays current
        behind the alert.

        ``force`` is for startup, where the state file may say an alert was
        active but nothing has been re-asserted on the sign yet.
        """
        labels = [slot.label.encode("ascii") for slot in self.list_slots()]

        if not force and self._alert_active():
            self._deferred_run_sequence = labels
            logger.debug(
                "an alert is holding the sign, so the run sequence is deferred until release"
            )
            return

        self._deferred_run_sequence = None
        await self._controller.set_run_sequence(labels)

    async def flush_deferred(self) -> bool:
        """Apply a run sequence that was held back during an alert.

        Called when the alert is released. Returns whether anything was waiting.
        """
        async with self._lock:
            labels = self._deferred_run_sequence
            if labels is None:
                return False
            self._deferred_run_sequence = None
            await self._controller.set_run_sequence(labels)

        logger.info("applied the run sequence that was deferred during the alert")
        return True

    def _save(self) -> None:
        self._store.save(self._state)


__all__ = [
    "DEFAULT_SLOT_KEY",
    "LayoutFull",
    "MessageRegistry",
    "MessageTooLong",
    "RegistryError",
    "UnknownSlot",
]
