"""The set of messages currently sharing the sign, and the variables they call.

A slot is a named place on the sign that a source owns. Home Assistant owns
``temperature``, a doorbell automation might own ``doorbell``, and each one
writes to its own slot without knowing or caring about the others. Writing to a
slot that already exists replaces it rather than adding another, so a source
that re-sends every five minutes does not accumulate anything.

Each slot lives in its own sign file, and the run sequence names the occupied
files in order. That is the whole rotation mechanism: the sign cycles them by
itself, so a slot appearing or disappearing costs one small write and nothing
after that.

A variable is a value in a STRING file of its own, which a slot's message calls
with ``<var:name>``. Changing a variable rewrites only its STRING file, which
does not blank the display or restart the message calling it, and one variable
can be called from any number of messages. Slots and variables share one lock,
because the rule that holds them together spans both: a variable cannot be
deleted while a message calls it. Its STRING file's label is written into every
calling message as raw bytes, so handing that label to another variable would
make those messages show the wrong value, with nothing on the sign to say so.

An alert can call variables too, and the same rule covers it. The alert service
writes the priority file itself, so it renders through :meth:`rendering`, which
holds this lock across the render and the write.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from readerboard.protocol.markup import references, render, render_value
from readerboard.protocol.tokens import MODE_BY_NAME
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout, LayoutFull
from readerboard.sign.state import ServiceState, SlotState, StateStore, VariableState
from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """Something was wrong with a request to change the registry."""


class UnknownSlot(RegistryError):
    """No slot by that name is registered."""


class MessageTooLong(RegistryError):
    """The rendered message does not fit the sign file allocated to it."""


class UnknownVariable(RegistryError):
    """No variable by that name exists."""


class VariableTooLong(RegistryError):
    """The rendered value does not fit the STRING file allocated to it."""


class VariableInUse(RegistryError):
    """A variable cannot be deleted while a message or the alert calls it."""


class VariablesDisabled(RegistryError):
    """The variable pool is empty, so there is nothing to put a variable in."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MessageRegistry:
    """Owns the slots, the variables, both file pools, and the run sequence."""

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

    def list_variables(self) -> list[VariableState]:
        """Every variable, by name."""
        return sorted(self._state.variables.values(), key=lambda variable: variable.name)

    def get_variable(self, name: str) -> VariableState:
        """One variable by name."""
        variable = self._state.variables.get(name)
        if variable is None:
            raise UnknownVariable("no variable named %r exists" % name)
        return variable

    def callers(self, name: str) -> list[str]:
        """Return the keys of the slots whose messages call a variable, in rotation order."""
        return [slot.key for slot in self.list_slots() if name in references(slot.message)]

    def alert_calls(self, name: str) -> bool:
        """Say whether the alert holding the sign calls a variable.

        Read from the state both services share rather than from anything the
        alert service reports, so there is nothing to fall out of step. The
        alert is recorded under this registry's lock, in :meth:`rendering`, so
        a check made under the same lock never sees an alert half raised.
        """
        alert = self._state.alert
        return alert is not None and name in references(alert.message)

    @property
    def occupancy(self) -> tuple[int, int]:
        """How many slots are used, and how many there are in total."""
        return len(self._state.slots), self._layout.slot_count

    @property
    def variable_occupancy(self) -> tuple[int, int]:
        """How many variables exist, and how many there is room for."""
        return len(self._state.variables), self._layout.variable_count

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
                self._state.variables = {}

            self._reattach_labels()
            # force, because the state file may say an alert was active but
            # nothing has been re-asserted on the sign yet.
            await self._rewrite_all(force=True)
            self._save()

    def _reattach_labels(self) -> None:
        """Re-establish which slot and variable owns which file, dropping what cannot come back.

        Two slots cannot: one whose file is outside the pool as it now stands,
        and one with no message, which an earlier version accepted. The second
        would otherwise be rewritten and left in the run sequence on every
        start, holding a file open around nothing while the sign cycled to it
        and showed nothing. A variable whose file is outside the pool cannot
        come back either.

        A slot calling a variable that did not come back stays. The call draws
        nothing, which is what the sign itself draws for a call to a STRING that
        is not there, and losing a whole message over one missing value would be
        the harsher outcome. An alert calling one stays too, with a warning of
        its own.
        """
        for name, variable in list(self._state.variables.items()):
            try:
                self._layout.variables.restore(name, variable.label.encode("ascii"))
            except ValueError:
                logger.warning(
                    "variable %r used file %s, which is outside the current pool; dropping it",
                    name,
                    variable.label,
                )
                del self._state.variables[name]

        for key, slot in list(self._state.slots.items()):
            if not slot.message:
                logger.warning("slot %r has no message; dropping it", key)
                del self._state.slots[key]
                continue

            try:
                self._layout.slots.restore(key, slot.label.encode("ascii"))
            except ValueError:
                logger.warning(
                    "slot %r used file %s, which is outside the current pool; dropping it",
                    key,
                    slot.label,
                )
                del self._state.slots[key]
                continue

            for name in references(slot.message):
                if name not in self._state.variables:
                    logger.warning(
                        "slot %r calls variable %r, which no longer exists; the call "
                        "will show nothing until the variable is written again",
                        key,
                        name,
                    )

        # The alert outlives a reallocation, since the priority file is outside
        # the pool, so it can be left calling a variable that did not come back.
        alert = self._state.alert
        for name in references(alert.message) if alert is not None else []:
            if name not in self._state.variables:
                logger.warning(
                    "the alert calls variable %r, which no longer exists; the call will "
                    "show nothing until the variable is written again",
                    name,
                )

    async def _rewrite_all(self, *, force: bool = False) -> None:
        # Variables first, so that no message is ever drawn calling a STRING that
        # is not written yet, then the messages, then the run sequence that
        # starts playing them.
        for variable in self.list_variables():
            await self._write_variable(variable)
        for slot in self.list_slots():
            # A hidden slot's file is written too, so that the sign and the
            # suppression cache both carry its text and switching it back on
            # stays one write. :meth:`_hide` has the whole of it, including
            # when that turns into a blank instead.
            if slot.active:
                await self._write_slot(slot)
            else:
                await self._hide(slot)
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
        Rewriting a TEXT file restarts the message in it, so every message
        restarts once a refresh; variables spare the display a blank on every
        change of value, not on this.

        This could become a read-back comparison that only writes on a real
        mismatch: the frame builders for those reads exist, and the sign has
        since been shown to answer them through the Ethernet adapter. Nothing
        here depends on that yet. See "Reading state back" in
        docs/protocol-notes.md.
        """
        async with self._lock:
            self._controller.forget_sign_contents()
            await self._rewrite_all()
            self._dirty = False

        logger.debug(
            "re-pushed %d slot(s) and %d variable(s) to the sign",
            len(self._state.slots),
            len(self._state.variables),
        )

    async def reboot(self) -> int:
        """Reset the sign to recover it, then restore the rotation from record.

        This is the recovery path for a sign that has stopped showing what it
        was told, the wedged-decoder state a BetaBrite mounted out of reach can
        fall into when a stray bit corrupts what it is displaying and it cannot
        be power cycled by hand. It sends the same memory clear the one
        dangerous operation does, which erases every file on the sign and puts
        it through a reset, waits for that reset to finish, then re-pushes every
        variable, every slot and the run sequence.

        The service's own record is left untouched, so the sign comes back
        showing what it should rather than blank. Returns how many slots were
        restored. An alert lives in the priority file, which this does not
        touch; the caller re-asserts it.
        """
        async with self._lock:
            # apply_memory_config holds the sign's lock through the clear, the
            # configuration and the sign's power-up diagnostics, so it returns
            # only once the sign is listening again and the rewrite below cannot
            # land on a deaf sign.
            await self._controller.apply_memory_config(self._layout.allocations())
            self._state.layout = self._layout.as_applied()
            # apply_memory_config already forgot the sign's contents. Doing it
            # again is belt and braces: after a reset the cache is exactly what
            # cannot be trusted, and the rewrite below must not be suppressed.
            self._controller.forget_sign_contents()
            await self._rewrite_all(force=True)
            self._dirty = False
            self._save()

        count = len(self._state.slots)
        logger.warning(
            "sign rebooted; %d slot(s) and %d variable(s) restored",
            count,
            len(self._state.variables),
        )
        return count

    @property
    def in_sync(self) -> bool:
        """Whether everything registered is believed to be on the sign."""
        return not self._dirty

    @contextlib.asynccontextmanager
    async def rendering(self) -> AsyncIterator[Callable[..., bytes]]:
        """Hold the variables still while a message calling them is rendered and written.

        For the alert service, which writes the priority file itself. It gets
        the renderer a slot's message goes through, so an alert is refused for
        the same reasons and in the same words, and it keeps this lock until the
        write is done: a variable deleted between the render and the write
        would leave the alert calling a file the next variable could be given.

        The renderer takes a message and ``strict``, as :func:`render` does.
        Nothing that holds this may wait on anything that takes this lock, and
        releasing an alert does, through the run sequence it may have held back.
        """
        async with self._lock:
            yield self._render_message

    # == changing slots =====================================================

    async def upsert(
        self,
        key: str,
        message: str,
        *,
        mode: str,
        order: int = 0,
        ttl_seconds: float | None = None,
        delete_on_expiry: bool = True,
        source: str | None = None,
    ) -> SlotState:
        """Register or replace a slot and put it on the sign.

        Replacing a slot leaves it as active or inactive as it already was.
        Whether a message is showing is not part of the message, and a source
        that re-sends the same content every few minutes would otherwise switch
        one back on every time it did. :meth:`set_active` is the only way that
        moves.
        """
        mode_token = MODE_BY_NAME[mode]

        async with self._lock:
            # Rendered under the lock, because a message that calls a variable
            # renders to that variable's label, and a variable deleted between
            # the render and the write would leave the message calling a file
            # somebody else may be handed next.
            body = self._render_message(message)
            if len(body) > self._layout.slot_capacity:
                raise MessageTooLong(
                    "the message renders to %d bytes but each slot holds %d. Shorten it, or "
                    "raise slot_capacity and restart, which reallocates the sign and clears it."
                    % (len(body), self._layout.slot_capacity)
                )

            existed = key in self._state.slots
            previous = self._state.slots.get(key)
            label = self._layout.slots.assign(key)  # raises LayoutFull when the pool is full

            now = self._now()
            slot = SlotState(
                key=key,
                label=label.decode("ascii"),
                message=message,
                mode=mode,
                order=order,
                active=previous.active if previous is not None else True,
                delete_on_expiry=delete_on_expiry,
                source=source,
                expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
                updated_at=now,
            )

            self._state.slots[key] = slot
            try:
                # A hidden slot's new text goes to the sign as well, into a
                # file nothing is cycling to. That is what keeps switching it
                # back on down to one write; :meth:`_hide` has the reasoning.
                if slot.active:
                    await self._controller.write_text_file(
                        label, body, mode=mode_token.value
                    )
                else:
                    await self._hide(slot)
                if not existed:
                    await self._apply_run_sequence()
            except Exception:
                # The write did not land, so the slot is not on the sign, and
                # keeping it would promise what the sign is not showing. Put the
                # registry back exactly as it was, then let the error surface: a
                # sign that cannot be reached becomes the 503 that tells a client
                # it cannot write right now rather than a 200 that hides it. A new
                # message is theirs to retry when the link is back, and a failed
                # update leaves the previous one in place. The slots already on
                # the sign are re-pushed on reconnect regardless of this.
                if previous is not None:
                    self._state.slots[key] = previous
                else:
                    del self._state.slots[key]
                    self._layout.slots.release(key)
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

    async def set_active(self, key: str, active: bool) -> SlotState:
        """Put a slot into the rotation or take it out, without unregistering it.

        An inactive slot keeps its file, its order, its message and its name.
        It is left out of the run sequence, and that is the whole of what makes
        it inactive: the sign cycles what the sequence names and nothing else.

        Its file keeps its text, so switching it back on is one run sequence
        write and nothing else: the controller still holds those exact bytes
        for that file and declines to write them again. A run sequence written
        while the rotation was on screen was measured leaving it running with
        no blank and no restart, so a slot joins or leaves the rotation without
        disturbing the messages around it.

        The exception is the last one. A sign handed a run sequence naming
        nothing freezes on the message it was drawing and holds it indefinitely,
        measured on 2026-09-11 and recorded in docs/protocol-notes.md, so when
        no slot is left playing the file is emptied to break that. Switching one
        back on from there costs the text and the sequence, two writes, which is
        the price of coming back from a display that is holding nothing.
        """
        async with self._lock:
            slot = self._state.slots.get(key)
            if slot is None:
                raise UnknownSlot("no slot named %r is registered" % key)
            if slot.active == active:
                return slot

            slot.active = active
            if active:
                try:
                    # Written before the sequence names it, so the sign cannot
                    # cycle to a file that is still empty.
                    await self._write_slot(slot)
                    await self._apply_run_sequence()
                except Exception:
                    # Nothing landed, so claiming it is showing would be a lie.
                    slot.active = False
                    raise
            else:
                try:
                    await self._apply_run_sequence()
                    await self._hide(slot)
                except TransportError as err:
                    # Taking something off is like removing it: the intent
                    # stands and the next refresh carries it out.
                    self._dirty = True
                    logger.warning(
                        "slot %r deactivated but the sign is unreachable (%s)", key, err
                    )
            self._save()

        logger.info(
            "slot %r %s", key, "put back into the rotation" if active else "taken off the display"
        )
        return slot

    async def remove(self, key: str) -> None:
        """Take a slot off the sign and forget it."""
        async with self._lock:
            slot = self._state.slots.pop(key, None)
            if slot is None:
                raise UnknownSlot("no slot named %r is registered" % key)

            self._layout.slots.release(key)
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
                self._layout.slots.release(slot.key)

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
        """Act on slots whose TTL has passed, and let expired variables go stale.

        What the deadline does is the slot's own ``delete_on_expiry``: True hands
        its file back to the pool, False keeps it registered and takes it off
        the display, so it can be switched on again without being registered
        afresh. Returns the keys of every slot whose deadline fired, whichever
        of the two happened to it.

        A variable is never dropped here: messages call it, so it is given its
        stale value instead, which :meth:`_expire_variables` explains.
        """
        now = self._now()
        async with self._lock:
            await self._expire_variables(now)

            expired = [
                slot
                for slot in self._state.slots.values()
                if slot.expires_at is not None and slot.expires_at <= now
            ]
            if not expired:
                return []

            dropped = [slot for slot in expired if slot.delete_on_expiry]
            deactivated = [slot for slot in expired if not slot.delete_on_expiry]

            for slot in dropped:
                del self._state.slots[slot.key]
                self._layout.slots.release(slot.key)
            for slot in deactivated:
                slot.active = False
                # Cleared, or the slot would expire again the moment it was
                # switched back on.
                slot.expires_at = None

            # Taking the labels out of the run sequence is what removes them
            # from the sign. A dropped slot's file stays allocated, because
            # reallocating to reclaim it would erase everything else; it is
            # emptied instead. A deactivated one keeps its text, since it keeps
            # its slot and may be switched back on.
            try:
                await self._apply_run_sequence()
                for slot in dropped:
                    await self._blank(slot)
                for slot in deactivated:
                    await self._hide(slot)
            except TransportError as err:
                self._dirty = True
                logger.warning("slots expired but the sign is unreachable (%s)", err)
            self._save()

        if dropped:
            logger.info(
                "slot(s) %s expired and were removed", ", ".join(slot.key for slot in dropped)
            )
        if deactivated:
            logger.info(
                "slot(s) %s expired and were taken off the display, keeping their slots",
                ", ".join(slot.key for slot in deactivated),
            )
        return [slot.key for slot in expired]

    # == changing variables =================================================

    async def put_variable(
        self,
        name: str,
        value: str,
        *,
        ttl_seconds: float | None = None,
        stale_value: str = "",
        source: str | None = None,
    ) -> VariableState:
        """Create or change a variable and put its value on the sign.

        Only the variable's own STRING file is written. Every message calling it
        shows the new value the next time the sign draws it, without blanking or
        restarting, which is the whole reason variables exist.
        """
        self._require_variables()
        data = self._render_value(value, "value")
        # Checked now rather than when the TTL runs out, since by then there is
        # nobody to tell that it does not fit.
        self._render_value(stale_value, "stale_value")

        async with self._lock:
            existed = name in self._state.variables
            previous = self._state.variables.get(name)
            label = self._layout.variables.assign(name)  # raises LayoutFull when full

            now = self._now()
            variable = VariableState(
                name=name,
                label=label.decode("ascii"),
                value=value,
                stale_value=stale_value,
                source=source,
                expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
                updated_at=now,
            )

            self._state.variables[name] = variable
            try:
                await self._controller.write_string_file(label, data)
            except Exception:
                # As in upsert: a value that did not reach the sign must not be
                # recorded as though it had.
                if previous is not None:
                    self._state.variables[name] = previous
                else:
                    del self._state.variables[name]
                    self._layout.variables.release(name)
                raise

            self._save()

        logger.info(
            "variable %r %s in file %s%s",
            name,
            "updated" if existed else "created",
            variable.label,
            " from %s" % source if source else "",
        )
        return variable

    async def remove_variable(self, name: str) -> None:
        """Delete a variable, refusing while any message or the alert calls it."""
        async with self._lock:
            variable = self.get_variable(name)
            callers = self.callers(name)
            alert = self.alert_calls(name)
            if callers or alert:
                raise VariableInUse(_in_use(name, callers, alert=alert))

            del self._state.variables[name]
            self._layout.variables.release(name)
            try:
                # Emptied so that the next variable handed this file does not
                # have its first value suppressed as already written, which is
                # the same reason a released slot is blanked.
                await self._controller.write_string_file(variable.label.encode("ascii"), b"")
            except TransportError as err:
                self._dirty = True
                logger.warning("variable %r deleted but the sign is unreachable (%s)", name, err)
            self._save()

        logger.info("variable %r deleted, freeing file %s", name, variable.label)

    async def _expire_variables(self, now: datetime) -> None:
        """Show each variable whose TTL has passed as its stale value.

        A variable fed by a sensor that has stopped reporting would otherwise
        go on showing its last reading as though it were current, which is
        worse than showing nothing. It is not deleted, because messages call
        it; it shows ``stale_value`` until a fresh value is written. The caller
        holds the lock.
        """
        expired = [
            variable
            for variable in self._state.variables.values()
            if variable.expires_at is not None and variable.expires_at <= now
        ]
        if not expired:
            return

        for variable in expired:
            variable.stale = True
            variable.expires_at = None

        try:
            for variable in expired:
                await self._write_variable(variable)
        except TransportError as err:
            self._dirty = True
            logger.warning("variables went stale but the sign is unreachable (%s)", err)
        self._save()

        for variable in expired:
            logger.info(
                "variable %r%s went stale and now shows %r",
                variable.name,
                " from %s" % variable.source if variable.source else "",
                variable.stale_value,
            )

    # == internals ==========================================================

    def _variable_labels(self) -> dict[str, bytes]:
        """Which STRING file each variable lives in, for rendering the messages that call them."""
        return {
            name: variable.label.encode("ascii") for name, variable in self._state.variables.items()
        }

    def _require_variables(self) -> None:
        if not self._layout.variable_count:
            raise VariablesDisabled(
                "variables are switched off, because variable_count is 0. Raise it and "
                "restart, which reallocates the sign and clears it."
            )

    def _render_message(self, message: str, *, strict: bool = True) -> bytes:
        """Render a message with the variables that exist right now.

        Strictly for a message being accepted, and leniently for one accepted
        earlier and being written again, where a call to a variable that has
        since gone draws nothing, as the sign itself draws it.
        """
        if strict and references(message):
            self._require_variables()
        return render(message, strict=strict, variables=self._variable_labels())

    def _render_value(self, value: str, field: str) -> bytes:
        """Render a value strictly and make sure it fits its STRING file."""
        data = render_value(value)
        if len(data) > self._layout.variable_capacity:
            raise VariableTooLong(
                "the %s renders to %d bytes but each variable holds %d. The sign does not "
                "cut a value short, it empties it, so shorten it, or raise "
                "variable_capacity and restart, which reallocates the sign and clears it."
                % (field, len(data), self._layout.variable_capacity)
            )
        return data

    async def _write_slot(self, slot: SlotState) -> None:
        # Content already accepted once is re-rendered leniently, so that a slot
        # restored from disk cannot fail to come back because the rules around
        # it tightened in the meantime.
        body = self._render_message(slot.message, strict=False)
        await self._controller.write_text_file(
            slot.label.encode("ascii"),
            body,
            mode=MODE_BY_NAME[slot.mode].value,
        )

    async def _write_variable(self, variable: VariableState) -> None:
        # Leniently, for the same reason as a slot. A lenient render can come
        # out longer than the strict one it was accepted as, since a token this
        # version no longer knows passes through as its own text, and the sign
        # empties a value that overruns its file. Emptying it here says so in
        # the log rather than leaving the sign to do it silently.
        data = render_value(variable.shown, strict=False)
        if len(data) > self._layout.variable_capacity:
            logger.warning(
                "variable %r no longer fits its file, which holds %d bytes; showing it "
                "empty. The text was: %r",
                variable.name,
                self._layout.variable_capacity,
                variable.shown,
            )
            data = b""
        await self._controller.write_string_file(variable.label.encode("ascii"), data)

    def _anything_playing(self) -> bool:
        """Whether the run sequence names anything at all.

        This is the question the freeze turns on, and it is asked of the state
        as it stands now, so a caller that has just switched the last slot off
        gets False.
        """
        return any(slot.active for slot in self._state.slots.values())

    async def _hide(self, slot: SlotState) -> None:
        """Settle the file of a slot that is registered but not playing.

        Its text is left where it is while anything else is playing. The sign
        draws what the run sequence names and nothing else, so the bytes sit
        there unseen, and switching the slot back on is then one run sequence
        write and no more: the controller still holds those exact bytes for
        that file and declines to write them again. That is the write the spike
        measured leaving the rotation running with no blank and no restart.

        Emptying it instead would cost a second write to put the text back, and
        rewriting a TEXT file restarts the message in it, which is a visible
        blink for nothing. The service did empty it until 2026-09-12, when
        toggling a slot on a real sign was watched doing exactly that.

        The file is emptied only when nothing is playing at all. A sign handed a
        run sequence naming nothing freezes on the message it was drawing and
        holds it indefinitely, so with the rotation empty the file has to go
        blank or that message stays on the display for good.
        """
        if self._anything_playing():
            await self._write_slot(slot)
        else:
            await self._blank(slot)

    async def _blank(self, slot: SlotState) -> None:
        """Empty a file that no longer holds a slot.

        The run sequence has already stopped naming it, which is enough while
        anything else is still named. It is not enough when that was the last
        one. A sign given a sequence that names nothing freezes on the message
        it was showing and holds it there, measured on 2026-09-11 and recorded
        in docs/protocol-notes.md, so this is what actually clears the display
        when the last slot goes rather than tidiness.

        It matters a second time when the file is handed to a different slot
        later, since a stale body would otherwise be what the suppression cache
        compares against.

        A slot that is merely hidden keeps its file, so it goes through
        :meth:`_hide` instead, which comes back here only when the rotation is
        empty and the freeze is the thing to break.
        """
        await self._controller.write_text_file(slot.label.encode("ascii"), b"")

    async def _apply_run_sequence(self, *, force: bool = False) -> None:
        """Tell the sign which files to play, unless an alert is holding it.

        The protocol says a running priority message is cancelled by a serial
        write to the run time table or the run day table, and says nothing
        either way about a write to the run sequence, so this took the cautious
        reading: a slot expiring during an alert would otherwise take the alert
        off the display with nothing to explain why.

        The spike settled it on 2026-09-11. The sign was given a real run
        sequence change with an alert up and kept the alert, so the deferral
        below is unnecessary and is waiting to be removed, along with
        ``flush_deferred``, the ``force`` flag and the alert service's release
        hook.

        Until then, while an alert is up the sequence is remembered and applied
        when the sign is handed back. Writing a slot's own TEXT file is not on
        the protocol's list and carries on as normal, so content stays current
        behind the alert. Nor is writing a variable's STRING file, and on this
        sign that was measured leaving an alert in place.

        ``force`` is for startup, where the state file may say an alert was
        active but nothing has been re-asserted on the sign yet.
        """
        # Only the active ones. An inactive slot keeps its file and its place in
        # the order and is simply not named here, which is the whole mechanism
        # behind switching a message off.
        labels = [slot.label.encode("ascii") for slot in self.list_slots() if slot.active]

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


def _in_use(name: str, callers: list[str], *, alert: bool) -> str:
    """Say what still calls a variable, and what to do about each."""
    holders = []
    remedies = []
    if callers:
        holders.append(
            "%s %s"
            % ("slot" if len(callers) == 1 else "slots", ", ".join(repr(key) for key in callers))
        )
        remedies.append(
            "change or remove %s" % ("that message" if len(callers) == 1 else "those messages")
        )
    if alert:
        holders.append("the alert holding the sign")
        remedies.append("release or replace the alert")
    return (
        "variable %r is called by %s. First %s; deleting it now would leave %s calling a file "
        "another variable could be given next."
        % (
            name,
            " and ".join(holders),
            " and ".join(remedies),
            "it" if len(callers) + alert == 1 else "them",
        )
    )


__all__ = [
    "LayoutFull",
    "MessageRegistry",
    "MessageTooLong",
    "RegistryError",
    "UnknownSlot",
    "UnknownVariable",
    "VariableInUse",
    "VariableTooLong",
    "VariablesDisabled",
]
