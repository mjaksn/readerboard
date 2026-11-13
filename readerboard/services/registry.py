"""The set of slots currently sharing the sign, and the variables they call.

A slot is a named place on the sign that a source owns. Home Assistant owns
``temperature``, a doorbell automation might own ``doorbell``, and each one
writes to its own slot without knowing or caring about the others. Writing to a
slot that already exists replaces it rather than adding another, so a source
that re-sends every five minutes does not accumulate anything.

Each slot lives in its own sign file, and the run sequence names the files of
the slots that are showing, in order. That is the whole rotation mechanism: the
sign cycles them by itself, so a slot appearing or disappearing costs one small
write and nothing after that.

A variable is a value in a STRING file of its own, which a slot's message calls
with ``<var:name>``. Changing a variable rewrites only its STRING file, which
does not blank the display or restart the message calling it, and one variable
can be called from any number of messages. A variable a message calls cannot be
deleted: its STRING file's label is written into every calling message as raw
bytes, so handing that label to another variable would make those messages show
the wrong value, with nothing on the sign to say so.

All three pools here share one lock, because the rules holding them together
span more than one of them. That rule about variables is the first, and the
picture rules below are the rest.

An icon is a bitmap in a picture file, which a message draws with
``<icon:name>``. It works unlike either of the other two and the difference is
the thing to understand before reading the picture methods below. Nobody creates
an icon: there are 148 built in, `picture_count` says how many can be on the
sign at once, and a file is claimed by whichever icon a message happens to call.

Two rules follow from that, and both come from one measured fact: writing a
picture blanks the display and restarts a scrolling message, which writing a
STRING does not.

- **A picture file is released lazily**, when another icon needs it and not when
  its last caller goes. Freeing it eagerly would spend a blank on tidiness, and
  a source that alternates between two icons would pay for every switch.
- **Which icons are in use counts hidden slots and the alert**, not only what is
  on screen. A hidden slot is shown again by one run sequence write, and that is
  only true while its icons still have their files.

An alert can call variables and icons too, and the same rules cover it. The
alert service writes the priority file itself, so it renders through
:meth:`rendering`, which holds this lock across the render and the write.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from readerboard import icons
from readerboard.protocol import replies
from readerboard.protocol.constants import PICTURE_FILE_LABELS
from readerboard.protocol.markup import icon_references, references, render, render_value
from readerboard.protocol.tokens import MODE_BY_NAME
from readerboard.sign import pool
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout, LayoutFull
from readerboard.sign.state import (
    PictureState,
    ServiceState,
    SlotState,
    StateStore,
    VariableState,
)
from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)

# How long the refresh goes without asking the sign a question after one went
# unanswered. It bounds two costs at once, whatever the refresh interval is: the
# sign simulator, which answers no read, stalls one read's three second deadline
# per period with the sign's lock held, and a sign switched off behind a
# connected adapter has every picture rewritten on every refresh for at most
# this long after it comes back. It matches the default refresh interval, so a
# service left at that default asks every refresh and the simulator costs three
# seconds in fifteen minutes; it is shorter intervals that this spares.
ASK_AGAIN_AFTER = timedelta(minutes=15)


class RegistryError(Exception):
    """Something was wrong with a request to change the registry."""


class SignNotRestored(TransportError):
    """The startup restore still owes the sign its file pool, so nothing can be written yet.

    A :class:`TransportError` rather than a :class:`RegistryError`, because it
    says the sign is not ready rather than that the request was wrong: it is a
    503 like an unreachable sign, the loops that already treat an unreachable
    sign as a reason to wait treat this the same way, and the alert queue keeps
    a waiting alert rather than dropping it as one that could not be shown.
    """


class UnknownSlot(RegistryError):
    """No slot by that name is registered."""


class MessageTooLong(RegistryError):
    """The rendered message does not fit the sign file allocated to it."""


class UnknownVariable(RegistryError):
    """No variable by that name exists."""


class VariableTooLong(RegistryError):
    """The rendered value does not fit the STRING file allocated to it."""


class VariableInUse(RegistryError):
    """A variable cannot be deleted while a message, the alert, or a waiting alert calls it."""


class VariablesDisabled(RegistryError):
    """The variable pool is empty, so there is nothing to put a variable in."""


class IconsDisabled(RegistryError):
    """The picture pool is empty, so no icon can be drawn."""


class PicturePoolFull(RegistryError):
    """Every picture file is holding an icon something still calls.

    Not the same failure as a full slot pool, and the difference is what the
    message has to get across. A picture file is never held by an icon nobody
    calls, since one of those is given up the moment another icon wants it. So
    this means the messages and the alert now on the sign between them ask for
    more different icons than there is room for at once.
    """


def _picture_key(name: str, tint: str | None) -> str:
    """Name the picture holding one icon in one tint, as a message writes it.

    ``sun`` or ``check:red``, which is the body of the tag itself. A tint gets a
    picture of its own because it changes the bitmap, and using the tag body as
    the key means what is in the state file and in a log line reads as the thing
    somebody wrote in a message.
    """
    return name if tint is None else "%s:%s" % (name, tint)


@dataclass
class _Claims:
    """What one message's icons took from the picture pool, so it can be undone.

    Two lists rather than one, because a claim can cost two things. ``claimed``
    is the files newly given to this message's icons. ``evicted`` is the
    pictures that were given up to make room, which is the part that is easy to
    miss: a write that fails after an eviction would otherwise leave the icon
    that was evicted with no file and no way back, since nothing rewrites a
    message whose text has not changed.
    """

    claimed: list[str] = field(default_factory=list)
    evicted: list[PictureState] = field(default_factory=list)


class _Rendering:
    """What :meth:`SlotRegistry.rendering` hands the alert service.

    Calling it renders, which is all it used to be. Drawing the icons is a
    second step on purpose, because the caller has a rule of its own to apply in
    between: an alert is refused for being too long only after it has rendered,
    and writing its icons before that refusal would draw them into files the
    alert still on the sign is calling, leaving it showing the wrong picture
    until something refreshed it.

    So the order is render, decide, then draw. A caller that never draws has
    claimed files and put nothing in them, which is why the two paths that
    re-assert an alert already on the sign are given no message and claim
    nothing, and why neither is an alert that is only checked before it joins
    the queue.
    """

    def __init__(
        self,
        render: Callable[..., bytes],
        draw: Callable[[], Awaitable[None]],
        revert: Callable[[], Awaitable[None]],
        *,
        check: Callable[[str], bytes],
        draws_pictures: bool = False,
    ) -> None:
        """Hold the renderer, the write that has not happened yet, and its undo."""
        self._render = render
        self._check = check
        self._draw = draw
        self._revert = revert
        self._draws_pictures = draws_pictures

    @property
    def draws_pictures(self) -> bool:
        """Whether :meth:`draw_icons` has anything to write.

        The alert service reads it to decide whether the sign has to be handed
        back before that write. A picture is not taken while a priority message
        is running, and an alert replacing an alert draws under the one still
        up; without this every alert that replaced another would hand the sign
        back and take it again for no picture at all.
        """
        return self._draws_pictures

    def __call__(self, message: str, *, strict: bool = True) -> bytes:
        """Render a message, exactly as :meth:`SlotRegistry._render_message` does."""
        return self._render(message, strict=strict)

    def check(self, message: str) -> bytes:
        """Render a message as it would render once its icons had files, claiming none.

        For an alert being queued rather than shown. It is refused now for
        everything about the alert itself that would refuse it at the head of
        the queue, an unknown icon or variable, a body too long, or more icons
        than the sign has picture files, while its caller is still there to
        hear about it. Its pictures are claimed when it takes the sign, because
        a queue holding picture files for alerts not yet showing would empty the
        pool of the ones the display is using. So a pool that is full of the
        slots' icons when its turn comes is not checked here, and drops it then.
        """
        return self._check(message)

    async def revert(self) -> None:
        """Give back what this claimed, and put back on the sign what it evicted.

        For a caller that has to undo before it writes anything else, which is
        the alert service putting back the alert this one was replacing. The
        registry undoes its own claims when the failure reaches it, but that is
        too late for a caller that writes to the display on the way past: with a
        full pool, claiming this message's icon can evict one only the alert
        being replaced calls, and putting that alert back before the record is
        restored writes it to the sign without its icon.

        Doing it here, under both locks, is also what stops the pool moving
        between giving the file back and drawing the old bitmap into it.
        """
        await self._revert()

    async def draw_icons(self) -> None:
        """Write the newly claimed pictures to the sign. Call it once, after deciding."""
        await self._draw()


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Lift(Protocol):
    """What a priority hold yields: a place to read afterwards what it did.

    Read it on the way out rather than on the way in, because what it reports
    is not known until then. Whether an alert goes back on the sign is decided
    when the hold closes: one can be released or replaced while the sign is
    handed back, and one whose deadline passed in the meantime is dropped
    rather than shown again for a second.

    The mirror of :class:`readerboard.services.alerts.Renderer`, which the alert
    service declares and this module implements. This one goes the other way.
    """

    @property
    def put_the_alert_back(self) -> bool:
        """Whether an alert was written to the priority file as the hold closed."""
        ...


# What :meth:`SlotRegistry.set_priority_hold` takes: something that hands the
# sign back for the duration, given the renderer to put the alert back with.
# The alert service's own lock is below this registry's, so the renderer has to
# be passed in rather than reached for; see :meth:`AlertService.lifted`.
PriorityHold = Callable[[Callable[..., bytes]], AbstractAsyncContextManager[Lift]]


class _NoLift:
    """The lift a registry with no alert service wired in gets."""

    @property
    def put_the_alert_back(self) -> bool:
        """Nothing was handed back, so nothing was put back."""
        return False


class SlotRegistry:
    """Owns the slots, the variables, both file pools, and the run sequence."""

    def __init__(
        self,
        controller: SignController,
        layout: Layout,
        store: StateStore,
        state: ServiceState,
        *,
        now: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Wire the registry to the sign and to the state it was restored from."""
        self._controller = controller
        self._layout = layout
        self._store = store
        self._state = state
        self._now = now
        self._lock = asyncio.Lock()
        self._dirty = False
        self._restore_owed = False
        # Why the reconnect could not finish an owed restore, when the sign
        # answered that it has no room for the pool, so that a refused write
        # names that rather than a reconnect that has already happened.
        self._too_small_for_the_pool: str | None = None
        self._priority_hold: PriorityHold | None = None
        # When asking the sign a question is worth doing again, or None while it
        # is worth doing every time. The sign simulator decodes reads and
        # answers none of them, so without this every refresh against it would
        # wait out the read's three second deadline, with the sign's lock held,
        # and then do exactly what it would have done anyway. One unanswered
        # read is enough to stop asking for ASK_AGAIN_AFTER, or until the link
        # comes back from being down: see :meth:`ask_the_sign_again`.
        self._ask_again_at: datetime | None = None

    def set_priority_hold(self, hold: PriorityHold) -> None:
        """Give the registry a way to hand the sign back while it writes a picture.

        :meth:`AlertService.lifted` in the service. Without one, a picture
        written while an alert is up is lost, which is what this exists for;
        with none set the registry writes as it always did, which is what the
        tests that have no alert service want.
        """
        self._priority_hold = hold

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

    def waiting_alerts_calling(self, name: str) -> list[str | None]:
        """Return the keys of the waiting alerts that call a variable, None for one with no key."""
        return [
            waiting.key
            for waiting in self._state.alert_queue
            if name in references(waiting.message)
        ]

    def waiting_alerts_call(self, name: str) -> bool:
        """Say whether any alert waiting its turn calls a variable.

        A waiting alert is checked against the variables when it is queued, and
        rendered against them again when it takes the sign. Deleting one in
        between would leave it calling a file the next variable could be given,
        so it holds the variable exactly as the alert on the sign does. It holds
        no picture file, though: those are claimed when it takes the sign.
        """
        return bool(self.waiting_alerts_calling(name))

    @property
    def occupancy(self) -> tuple[int, int]:
        """How many slots are used, and how many there are in total."""
        return len(self._state.slots), self._layout.slot_count

    @property
    def variable_occupancy(self) -> tuple[int, int]:
        """How many variables exist, and how many there is room for."""
        return len(self._state.variables), self._layout.variable_count

    @property
    def picture_occupancy(self) -> tuple[int, int]:
        """How many picture files hold an icon, and how many there are.

        The first number counts files that have been written, not icons anything
        still calls. A picture is kept after its last caller goes, so a full
        pool is the ordinary resting state rather than a warning.
        """
        return len(self._state.pictures), self._layout.picture_count

    # == startup ============================================================

    async def restore(self) -> None:
        """Bring the sign back to what the state file says it should be showing.

        Reconfiguring memory is avoided unless the pool itself changed, because
        it erases the sign. On the ordinary restart, where nothing changed, this
        rewrites the files and the run sequence and nothing is erased.

        The one other reason is ``reallocation_pending`` in the state file. A
        reboot saves it before it sends anything and clears it once the
        configuration has gone out, so it is set when a reboot stopped before
        that, having cleared the sign or not having reached it at all. It is
        also left set when the pool was written, by a reboot or by the start
        that finishes one, and every save after it failed. The record cannot
        tell these apart. In each the pool is written again, but the pool has
        not changed and the record is the service's own, so every slot, variable
        and picture is kept and written back.

        A restore that has to write the pool and cannot, because the sign is not
        reachable at startup, leaves the restore owed: see
        :attr:`restore_owed`. The reconnect hook in ``app.py`` finishes it once
        the link comes up, through :meth:`finish_owed_restore`, which asks the
        sign for its room first; calling this again directly would not.
        """
        async with self._lock:
            await self._restore_locked()

    async def _restore_locked(self) -> bool:
        """Do the whole of :meth:`restore`, with the lock already held.

        Returns whether an alert was put back as part of the rewrite, on the
        same terms as :meth:`refresh`, for :meth:`reboot`, which finishes an
        owed restore this way.
        """
        if self._layout.needs_reconfiguration(self._state.layout):
            logger.info("the sign's file pool has changed, so it must be reallocated")
            # Owed until the pool has gone out. The records still name the
            # files of the pool the sign held before, which the layout has
            # not been told about, so nothing may be handed a file until then:
            # it could be one a record still holds. They are not reattached
            # here, because they are emptied the moment the new pool goes out,
            # and reattaching against the new layout would drop records the
            # sign is still showing from the old one.
            self._restore_owed = True
            await self._controller.apply_memory_config(self._layout.allocations())
            self._state.layout = self._layout.as_applied()
            self._state.reallocation_pending = False
            # Every file was just erased, so nothing survives from before.
            self._state.slots = {}
            self._state.variables = {}
            self._state.pictures = {}
            self._restore_owed = False
            self._reattach_labels()
        elif self._state.reallocation_pending:
            logger.warning(
                "the state file says a sign reboot may not have written the sign's "
                "file pool, either because it stopped first or because it could not "
                "record that it had, so the pool is written again; every message is "
                "kept and put back"
            )
            # The pool has not changed and the records survive the write, so
            # they are given their files first, whether or not the write below
            # gets through.
            self._reattach_labels()
            # Out of step from the erase until the rewrite below finishes, so
            # a restore that stops in between, and a service that starts
            # anyway, reports it rather than a blank sign in sync.
            self._dirty = True
            self._restore_owed = True
            await self._controller.apply_memory_config(self._layout.allocations())
            self._state.reallocation_pending = False
            self._restore_owed = False
            # At once, so that a restore that fails partway does not leave
            # the next start writing the pool again for no reason.
            self._save_between_the_pool_and_the_rewrite()
        else:
            self._reattach_labels()

        put_the_alert_back = await self._rewrite_all()
        self._dirty = False
        self._save()
        return put_the_alert_back

    async def finish_owed_restore(self) -> None:
        """Do the restore the start could not, once the sign can be reached.

        That start was going to write a memory configuration, which erases every
        message on the sign, and was stopped only by the sign being out of reach.
        This is the same erase, owed rather than new, and it goes the same way:
        the sign is asked how big its pool is first, because at startup that
        question went unanswered and the check passed on the figure assumed in
        its place. With this, the paths that write a memory configuration are
        three, the startup, :meth:`reboot` and this, and each of them asks.

        Raises :class:`readerboard.sign.pool.PoolTooLarge` when the sign answers
        that it has no room, before anything is written, and the restore stays
        owed. Does nothing when no restore is owed.
        """
        async with self._lock:
            if not self._restore_owed:
                return
            budget = await pool.measure(
                self._controller, fallback=pool.ASSUMED_SIGN_MEMORY_POOL
            )
            try:
                pool.check_fits(self._layout, budget)
            except pool.PoolTooLarge as err:
                self._too_small_for_the_pool = str(err)
                raise
            self._too_small_for_the_pool = None
            await self._restore_locked()

    @property
    def restore_owed(self) -> bool:
        """Whether a startup restore still has to write the sign's file pool.

        True once a restore that had to write a memory configuration could not,
        until one does. Until then the sign may not have the files the service
        would hand out, and in the changed-pool case the records still name the
        files of the old pool, so everything that would hand out a file or
        write to one is refused with :class:`SignNotRestored`, and
        :attr:`in_sync` is false.
        """
        return self._restore_owed

    def _refuse_while_restore_owed(self) -> None:
        if not self._restore_owed:
            return
        if self._too_small_for_the_pool is not None:
            raise SignNotRestored(
                "the sign's file pool has not been written since the service started, "
                "and cannot be: %s Until it is, nothing that needs the sign's files can "
                "be written" % self._too_small_for_the_pool
            )
        raise SignNotRestored(
            "the sign's file pool has not been written since the service started, "
            "because the sign was not reachable then. It is written when the service "
            "next reaches the sign, and until then nothing that needs the sign's files "
            "can be written"
        )

    def _reattach_labels(self) -> None:
        """Re-establish which slot and variable owns which file, dropping what cannot come back.

        Two slots cannot: one whose file is outside the pool as it now stands,
        and one with no message, which an earlier version accepted. The second
        would otherwise be rewritten and left in the run sequence on every
        start, holding a file open around nothing. The sign gives an empty file
        no turn of its own, but it does hold the message before it several
        seconds longer, measured on 2026-09-12, so the slot would be spent and
        the rotation would drag for it without ever showing why. A variable
        whose file is outside the pool cannot come back either.

        A slot calling a variable that did not come back stays. The call draws
        nothing, which is what the sign itself draws for a call to a STRING that
        is not there, and losing a whole message over one missing value would be
        the harsher outcome. An alert calling one stays too, with a warning of
        its own.

        A picture cannot come back for either of two reasons, and both drop the
        record rather than the message. Its file may be outside the pool, which
        the pass below can fix by giving it another. Or the icon it held may not
        be in this version's library at all, which nothing can fix and which has
        to be caught here: every picture is written before any message on a
        restore, so one that cannot be drawn would raise there and take the
        whole restore with it.
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

        for picture_key, picture in list(self._state.pictures.items()):
            try:
                icons.resolve(picture.name, picture.tint)
            except icons.IconError as err:
                # The library is data and changes between versions, so a stored
                # picture can name an icon this one does not have. It has to go
                # here rather than later: every picture is written before any
                # message on a restore, and one that cannot be drawn would raise
                # there and take the whole restore with it, leaving a service
                # that does not come up. The message calling it stays and draws
                # nothing for the call, which is what the sign draws for a
                # picture that is not there.
                logger.warning(
                    "picture file %s held icon %r, which this version does not have (%s); "
                    "the messages calling it will show nothing where it was drawn",
                    picture.label,
                    picture_key,
                    err,
                )
                del self._state.pictures[picture_key]
                continue

            try:
                self._layout.pictures.restore(picture_key, picture.label.encode("latin-1"))
            except ValueError:
                # Unlike a variable, this is not a loss. An icon is not
                # somebody's data; it is one of a fixed set, and the pass below
                # gives it a file again if the messages still call it and there
                # is room.
                logger.info(
                    "icon %r used picture file %s, which is outside the current pool",
                    picture_key,
                    picture.label,
                )
                del self._state.pictures[picture_key]

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

    def _reclaim_pictures(self) -> bool:
        """Give a picture file back to every icon the surviving messages call.

        Returns whether it gave one back, which :meth:`refresh` needs. A file
        claimed here holds whatever the icon before it left in it, so the bitmap
        still has to be drawn, and the refresh is what draws it. Skipping the
        re-push after a claim would leave the message calling that icon drawing
        the wrong picture, or none.

        Narrower than it looks, and worth saying exactly what it is for. Most
        ways of losing a picture take the messages with them: changing
        ``picture_count`` reallocates the sign, and a reallocation erases the
        slots along with the files, so there is nothing left to reclaim for.

        What it covers is the case where a picture loses its file and the
        messages do not, and without it the message calling that icon would draw
        nothing for it for good, since nothing rewrites a message that has not
        changed. Three things can still leave it that way. A stored picture whose
        label is not in the pool, which ``_reattach_labels`` drops: a changed
        label set reallocates the sign now, so what is left is a state file that
        disagrees with the pool for some other reason. A claim that was rolled
        back after an eviction it could not undo, which :meth:`_undo_claims`
        logs when it happens. And a rollback that got the file back and could
        not draw the bitmap into it, which gives the file up rather than leave a
        record saying it holds a picture it does not; :meth:`rendering`'s revert
        logs that one.

        Which is why this runs from :meth:`_rewrite_all` rather than from the
        restart path alone: the third of those happens while the service is
        running, and the refresh is the next thing along that can repair it.

        The refusals it swallows are real rather than defensive, and the likely
        one is not a full pool. The icon library is data and changes between
        versions, so a stored message can call an icon this version no longer
        has. That is worth a line in the log and is not worth refusing to
        start over.

        Best effort, and deliberately so. A pool too small for what the messages
        between them ask for is a configuration to fix, not a reason to refuse
        to start: the service is expected to come back after a power cut without
        anybody logging in. What cannot be given a file draws nothing, which is
        what the sign itself draws for a call to a picture that is not there.
        """
        alert = self._state.alert
        wanted: list[tuple[str, str | None]] = []
        for slot in self.list_slots():
            wanted += icon_references(slot.message)
        if alert is not None:
            wanted += icon_references(alert.message)

        # Every icon the surviving messages call between them, so that giving a
        # file to one of them cannot take it from another.
        keeping = {_picture_key(name, tint) for name, tint in wanted}
        missing: list[str] = []
        reclaimed = False
        for name, tint in wanted:
            if _picture_key(name, tint) in self._state.pictures:
                continue
            try:
                self._claim_picture(
                    name,
                    tint,
                    keeping=keeping,
                    replacing=None,
                    replacing_alert=False,
                    # Nothing here has a write to fail, so there is nothing to
                    # put back: an eviction on this path is a file passing from
                    # an icon nothing calls to one something does.
                    evicted=[],
                )
            except (RegistryError, icons.IconError):
                key = _picture_key(name, tint)
                if key not in missing:
                    missing.append(key)
            else:
                reclaimed = True

        if missing:
            logger.warning(
                "no picture file for %s; those calls will show nothing until there is "
                "room. Raise picture_count and restart, which reallocates the sign and "
                "clears it.",
                ", ".join(missing),
            )
        return reclaimed

    async def _rewrite_all(self) -> bool:
        """Write everything the state file describes. Returns whether it put an alert back.

        The return is what stops the alert being written to the sign twice in a
        row. Handing the sign back for the picture writes means putting the
        alert straight back on afterwards, and that write is a real one: the
        alert restarts on the display. A caller that then re-asserts the alert
        restarts it a second time for nothing, which is the flicker that came
        with the hand-back. True here means the caller has nothing left to do.

        False covers both the case where no alert was up and the case where
        there were no pictures to write, so the sign was never handed back and
        nothing here touched the priority file. The second is why the caller
        still needs :meth:`AlertService.reassert`: a service with no icons goes
        through here without going near the priority file at all, and a sign
        power cycled mid-alert would stay blank without it.
        """
        # Before anything is written, because a message can outlive the picture
        # file its icon was in and nothing rewrites a message that has not
        # changed. Here rather than in the restore that used to call it, because
        # this is the one place all three rewriting paths go through: a refresh
        # and a reboot need the repair as much as a restart does, and a refresh
        # is what the rollback in ``rendering`` counts on to give an icon its
        # file back when it could not redraw the bitmap itself.
        self._reclaim_pictures()

        # Pictures and variables first, so that no message is ever drawn calling
        # a file that is not written yet, then the messages, then the run
        # sequence that starts playing them.
        #
        # The pictures, and only the pictures, are written with the sign handed
        # back. This is the path the bug was found on: a restart during an alert
        # rewrites everything while the sign is still showing the alert it kept
        # across the restart, so every picture write landed under a priority
        # message and none of them took.
        handed_the_alert_back = False
        if self._state.pictures:
            async with self._priority_lifted() as lifted:
                for picture in self._state.pictures.values():
                    await self._write_picture(picture)
            handed_the_alert_back = lifted.put_the_alert_back
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
        await self._apply_run_sequence()
        return handed_the_alert_back

    async def ask_the_sign_again(self) -> None:
        """Let the next refresh ask the sign a question, even if one went unanswered.

        Called when the link comes back up. An unanswered read stops the refresh
        asking, which is right for the sign simulator, but it cannot be told
        apart from a network adapter that has dropped off: a write to an adapter
        that has gone lands in the local socket buffer and returns, and the read
        after it hears nothing, with the link still looking up. What tells them
        apart is what happens next. The adapter's link goes down within a minute
        or so, and later comes back, and the simulator's never does. Without
        this, one adapter drop would leave a sign with icons blanking once for
        every picture on every refresh until :data:`ASK_AGAIN_AFTER` ran out.

        It only helps where a link drops. A sign switched off behind a connected
        adapter for longer than a refresh interval stops the asking with no
        reconnect to follow, and so does a sign power cycled while a refresh
        lands, or an outage that outlasts the read's deadline and is over before
        the connection gives up. Those wait out :data:`ASK_AGAIN_AFTER` instead,
        and every refresh until then rewrites every picture.

        It takes the lock so that a refresh already under way finishes first. A
        read that refresh had out could otherwise fail after the reset and stop
        the asking straight away again.
        """
        async with self._lock:
            self._ask_again_at = None

    async def repush_at_next_refresh(self) -> None:
        """Make the next refresh push everything again rather than ask first.

        Called when the link comes back up, including when a write had to open
        it again under itself. A write that went down the old connection after
        it died landed in a local buffer and went no further, and the picture
        the refresh would ask about says nothing about which file that was. The
        controller forgets what it believed the sign held, but that alone does
        not reach the refresh: a write between the reopen and the refresh fills
        the cache again, a picture among them, and the refresh would then ask
        about that picture, hear yes, and leave the lost write lost. Marking the
        record as not on the sign is what carries the repair across that gap,
        and only a completed re-push clears it.
        """
        async with self._lock:
            self._dirty = True

    async def refresh(self) -> bool:
        """Check that the sign still holds what it was given, and push it all again if not.

        This is the answer to a problem the suppression cache cannot see. The
        sign and the Ethernet adapter are separately powered, so the sign can be
        power cycled while the TCP link to the adapter stays up. No reconnect
        fires, the cache stays warm, and suppression then skips exactly the
        writes that would repair a sign which is now blank.

        A re-push drops what the controller believes about the sign's contents
        and writes it all again. The refresh runs on a timer, and on every
        reconnect. Rewriting a TEXT file restarts the message in it, so every
        message restarts once a re-push; variables spare the display a blank on
        every change of value, not on this.

        So it asks first, which is what :meth:`_sign_still_holds_its_pictures`
        is for, and re-pushes only when the answer is no or there is no answer.
        The re-push is expensive in the one currency that matters here: writing
        a picture blanks the whole display, so a sign with five icons on it would
        blank five times an interval, for a repair that is almost never needed.
        Asking costs a message held still nothing at all and a scrolling one
        about half a second of stall, measured on 2026-09-12.

        Returns whether the caller can leave the alert alone, which it needs so
        that it does not write the alert a second time: the sign restarts an
        alert on the display whenever it takes one. That is so when an alert was
        put back on the sign as part of the re-push, see :meth:`_rewrite_all`,
        and when the sign answered that it still holds its picture. The second
        rests on a measurement: a sign unplugged for half a minute in the middle
        of an alert came back showing it, on 2026-11-05, so the priority file
        goes with the rest of the sign's memory, and one answer stands in for it
        as it does for every other file. Re-asserting there restarted the alert
        once a refresh interval for as long as it was up.
        """
        async with self._lock:
            self._refuse_while_restore_owed()
            if await self._sign_still_holds_its_pictures():
                logger.debug(
                    "the sign still holds the picture it was given, so nothing was "
                    "re-pushed"
                )
                return True

            self._controller.forget_sign_contents()
            put_the_alert_back = await self._rewrite_all()
            self._dirty = False

        logger.debug(
            "re-pushed %d slot(s) and %d variable(s) to the sign",
            len(self._state.slots),
            len(self._state.variables),
        )
        return put_the_alert_back

    async def _sign_still_holds_its_pictures(self) -> bool:
        """Ask the sign whether a picture it was given is still there.

        One question, and it stands in for the whole sign. What the periodic
        re-push is guarding against is the sign being power cycled behind a
        still-connected adapter, and that takes the sign's memory with it or
        leaves it alone: there is no version of it that loses one file. So a
        picture file that still holds a bitmap says the memory survived, and one
        that does not says it did not.

        False on anything it is not sure about, which is what makes it safe to
        add: no answer, an answer that does not parse, a sign the link is down
        to, no picture to ask about, or anything already known to need repair.
        Every one of those falls through to the re-push that happened
        unconditionally before, so the worst this can do is cost a read.

        That last one is the subtle one and it is not optional. Every place that
        sets ``_dirty`` is a failure or a rollback, a write that went out and one
        after it that did not, and each of them leaves the repair to the next
        refresh by name: see the comment in :meth:`upsert` about the sign showing
        something the record does not describe. What the sign holds in one
        picture file says nothing about any of that, so asking would answer yes
        over a sign that is known to be wrong somewhere else.

        The repair in :meth:`_reclaim_pictures` runs first for a related reason.
        It can give a file back to an icon that lost one, and a record the
        controller has never written is exactly the case the re-push is there to
        finish; asking the sign about a different file would answer yes and
        leave that one undrawn.
        """
        if self._dirty:
            # Something is already known not to have landed, and the refresh is
            # what every one of those paths leaves the repair to.
            return False

        if self._reclaim_pictures():
            # A file was just given back to an icon that had lost one, and the
            # bitmap is not in it yet. The re-push is what draws it.
            return False

        if not self._state.pictures:
            # Nothing to ask about, so nothing to skip. A service with no icons
            # re-pushes on the timer exactly as it always has.
            return False

        if not self._controller.is_connected:
            # The read would spend its whole deadline finding that out, with the
            # sign's lock held, and the re-push is about to fail anyway.
            return False

        if self._ask_again_at is not None and self._now() < self._ask_again_at:
            # A read went unanswered not long ago. See ASK_AGAIN_AFTER.
            return False

        written = set(self._controller.cached_labels())
        if any(picture.label not in written for picture in self._state.pictures.values()):
            return False

        label = next(iter(self._state.pictures.values())).label.encode("latin-1")
        try:
            reply = await self._controller.read_picture(label)
            return bool(replies.picture_contents(reply, label))
        except TransportError as err:
            # Nothing came back at all, which is what the sign simulator does to
            # every read, what a sign that has stopped listening does, and what
            # an adapter that has dropped off the network looks like before its
            # link is declared down. Asking again every interval would cost the
            # deadline each time and answer nothing, so stop for a while: until
            # the link has been down and come back, which is the adapter case
            # and never the simulator's, or until ASK_AGAIN_AFTER has passed,
            # which is what brings back a sign that was only switched off.
            self._ask_again_at = self._now() + ASK_AGAIN_AFTER
            logger.info(
                "the sign did not answer a read (%s), so the periodic refresh will "
                "stop asking and go back to pushing everything every time. It asks "
                "again in %d minutes, or sooner if the link drops and comes back.",
                err,
                ASK_AGAIN_AFTER // timedelta(minutes=1),
            )
            return False
        except replies.ReplyError as err:
            # It answered, just not with something that parses. Worth another
            # try next time, and it costs no deadline to find out.
            logger.debug("could not read what picture %s holds: %s", label, err)
            return False

    async def reboot(self) -> bool:
        """Reset the sign to recover it, then restore the rotation from record.

        This is the recovery path for a sign that has stopped showing what it
        was told, the wedged-decoder state a BetaBrite mounted out of reach can
        fall into when a stray bit corrupts what it is displaying and it cannot
        be power cycled by hand. It sends the same memory clear the one
        dangerous operation does, which erases every file on the sign and puts
        it through a reset, waits for that reset to finish, then re-pushes every
        variable, every slot and the run sequence.

        The service's own record is left untouched, so the sign comes back
        showing what it should rather than blank. The one thing it adds is a
        mark, ``reallocation_pending``, saved before the clear and cleared once
        the configuration has gone out, so that a reboot stopped in between is
        finished by the next start rather than forgotten. An alert lives in the
        priority file, which this touches only when it hands the sign back to
        write a picture; the caller re-asserts it when this reports that it did
        not.

        Returns whether an alert was put back as part of the restore, on the
        same terms as :meth:`refresh`. How many slots came back is in the log
        line below and in :meth:`list_slots`.

        While a startup restore is owed, see :attr:`restore_owed`, this finishes
        that restore instead, which empties the record when the pool changed,
        as the start would have.

        Raises :class:`readerboard.sign.pool.PoolTooLarge` if the sign turns out
        not to have room for the configuration, in which case nothing has been
        written and the sign is left exactly as it was.
        """
        async with self._lock:
            # The pool is checked here for the same reason it is checked at
            # startup, and with finish_owed_restore these three are every path
            # that writes a memory configuration. A sign swapped for a
            # smaller one leaves a state file that still matches, so startup
            # asks nothing, and without this the first reboot would clear the
            # sign and write a pool it cannot hold.
            #
            # A sign too wedged to answer falls back to the assumption and the
            # reboot goes ahead, which is the point: recovering a sign that has
            # stopped talking is what this endpoint is for, and refusing to
            # recover it because it did not answer a question would be refusing
            # in exactly the case it exists for.
            budget = await pool.measure(
                self._controller, fallback=pool.ASSUMED_SIGN_MEMORY_POOL
            )
            pool.check_fits(self._layout, budget)

            # A startup restore that never wrote the pool is finished instead,
            # the same way the reconnect would have. Going on below would record
            # the new pool as applied while the records still name the files of
            # the old one, and nothing would ever fix that: the next start would
            # find the layout matching and restore them into the wrong files.
            if self._restore_owed:
                put_the_alert_back = await self._restore_locked()
                logger.warning(
                    "sign rebooted, finishing the restore the service could not do at "
                    "startup; %d slot(s) and %d variable(s) restored",
                    len(self._state.slots),
                    len(self._state.variables),
                )
                return put_the_alert_back

            # From here the request can stop anywhere, cancelled by a client
            # that went away or failed by the link, and the record has to say
            # what the sign may now be at each point. Both marks go on before
            # the clear, because a write cancelled in flight can still reach the
            # sign, and the controller cannot say whether any of it did. A
            # reboot that failed before a byte left therefore costs a degraded
            # /health and a start that writes the pool again, both harmless;
            # the other way round, a cleared sign believed intact, is what this
            # is here to stop. The pending reallocation is saved, since only a
            # restart can act on it, and a save that fails stops the clear.
            self._state.reallocation_pending = True
            self._dirty = True
            self._save()

            # apply_memory_config holds the sign's lock through the clear, the
            # configuration and the sign's power-up diagnostics, so it returns
            # only once the sign is listening again and the rewrite below cannot
            # land on a deaf sign.
            await self._controller.apply_memory_config(self._layout.allocations())
            self._state.layout = self._layout.as_applied()
            self._state.reallocation_pending = False
            self._save_between_the_pool_and_the_rewrite()
            # apply_memory_config already forgot the sign's contents. Doing it
            # again is belt and braces: after a reset the cache is exactly what
            # cannot be trusted, and the rewrite below must not be suppressed.
            self._controller.forget_sign_contents()
            # _dirty stays set until every file is back, so a reboot stopped
            # partway through this leaves the next refresh pushing everything
            # rather than asking about one picture it did manage to redraw.
            put_the_alert_back = await self._rewrite_all()
            self._dirty = False
            self._save()

        logger.warning(
            "sign rebooted; %d slot(s) and %d variable(s) restored",
            len(self._state.slots),
            len(self._state.variables),
        )
        return put_the_alert_back

    @property
    def in_sync(self) -> bool:
        """Whether everything registered is believed to be on the sign.

        Never while a reallocation is pending, whatever a refresh has pushed
        since: a sign that may have been cleared with no pool written may have no
        files for the push to land in, and only a restart or another reboot
        writes the pool. Nor while a startup restore is owed, see
        :attr:`restore_owed`.
        """
        return (
            not self._dirty
            and not self._state.reallocation_pending
            and not self._restore_owed
        )

    @contextlib.asynccontextmanager
    async def rendering(self, message: str | None = None) -> AsyncIterator[_Rendering]:
        """Hold the files still while a message calling them is rendered and written.

        For the alert service, which writes the priority file itself. It gets
        the renderer a slot's message goes through, so an alert is refused for
        the same reasons and in the same words, and it keeps this lock until the
        write is done: a variable deleted between the render and the write
        would leave the alert calling a file the next variable could be given.

        ``message`` is what is about to be rendered, and it is there for icons
        rather than variables. An icon has to be given a picture file and drawn
        into it before anything renders a call to it, and both are this
        registry's to do, so an alert that carries one says so on the way in.
        None renders whatever it is given against the files that already exist,
        which is what the two paths that re-assert an alert already written
        want: those must not claim anything, because failing to would be a
        reason not to put the sign back. An alert about to be queued is checked
        under None as well, since it claims its pictures only when it takes the
        sign.

        What is yielded is a :class:`_Rendering`: call it to render, and await
        its ``draw_icons`` when the alert has been accepted, which is what puts
        the newly claimed pictures on the sign. The two are separate because the
        caller refuses an alert too long for the priority file after it renders,
        and drawing first would put those pictures into files the alert already
        on the sign is calling.

        Nothing that holds this may wait on anything that takes this lock.
        """
        async with self._lock:
            claims = _Claims()
            if message is not None:
                # Only an alert being drawn claims files. The renders under
                # None, which re-assert an alert, release one or save, hand out
                # nothing and go on as before.
                self._refuse_while_restore_owed()
                claims = self._claim_pictures(message, replacing_alert=True)
            # Whether a picture write was started. Until one is, every file
            # still holds the bitmap it held before the claim, so undoing the
            # claim puts the record back exactly as the sign has it.
            drawing = False

            async def draw() -> None:
                nonlocal drawing
                drawing = True
                await self._write_claims(claims.claimed)

            async def revert() -> None:
                self._undo_claims(claims)
                # Taken and emptied before a single write, so that the handler
                # below finds nothing left to give back however this goes. It
                # runs when the failure reaches it, and undoing the same claims
                # twice would release files this has just handed back.
                evicted = list(claims.evicted)
                claims.claimed.clear()
                claims.evicted.clear()

                # The record coming back is not enough on its own. The file
                # holds whatever this message's icon drew into it, so an evicted
                # picture has to be drawn again or its owner is left calling a
                # file showing somebody else's bitmap.
                for picture in evicted:
                    if self._state.pictures.get(picture.key) is not picture:
                        # _undo_claims logs and skips a file it could not
                        # reclaim, and drawing into one of those would write
                        # over whatever holds it now.
                        continue
                    try:
                        await self._write_picture(picture)
                    except Exception:
                        # The bitmap did not go back, so stop saying the file
                        # holds it. Whoever renders next then leaves the icon
                        # out, which is what the sign draws for a picture that
                        # is not there, instead of calling a file with another
                        # icon's bitmap sitting in it. That second one is the
                        # only outcome here worth refusing: it puts a wrong
                        # picture on the display rather than a missing one.
                        logger.warning(
                            "could not draw icon %r again after a refused write, so "
                            "its file goes back to the pool",
                            picture.key,
                        )
                        self._state.pictures.pop(picture.key, None)
                        self._layout.pictures.release(picture.key)
                        self._dirty = True

            try:
                yield _Rendering(
                    self._render_message,
                    draw,
                    revert,
                    check=self._check_message,
                    draws_pictures=bool(claims.claimed),
                )
            except BaseException:
                # The alert did not land, so nothing calls these. They are given
                # up rather than left holding files against a message that is
                # not on the sign.
                #
                # BaseException rather than Exception, here and at every other
                # rollback below. A cancelled request raises CancelledError,
                # which is not an Exception, so a handler that caught only
                # Exception let a cancellation walk out holding whatever this
                # had claimed. A client that gives up mid-write is ordinary, and
                # enough of them emptied a pool that had nothing in it. Each of
                # these re-raises, so nothing is swallowed by widening it.
                #
                # An alert refused before it drew anything, by if_active fail or
                # for being too long, or one that backed off to wait its turn,
                # wrote nothing to any picture file. Calling the sign out of
                # sync for that would report a fault on /health and re-push
                # every picture at the next check, blanking the display for
                # each, for a request that changed nothing.
                self._undo_claims(claims, written=drawing)
                raise

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
        active: bool | None = None,
        source: str | None = None,
    ) -> SlotState:
        """Register or replace a slot and put it on the sign.

        ``active`` is three-valued on purpose. None, which is what a caller who
        does not mention it sends, leaves the slot as showing or hidden as it
        already was, so a source re-sending the same content every few minutes
        cannot switch back on something that was deliberately hidden. True and
        False say so explicitly and move it, which is what lets one call both
        write a message and put it up: a notification with a minute on it is
        ``active=True`` with a ``ttl_seconds`` and ``delete_on_expiry=False``,
        sent again in full the next time the thing it reports changes.

        A brand new slot with nothing said about it is active, since registering
        a message nobody asked to hide means to show it.
        """
        mode_token = MODE_BY_NAME[mode]

        async with self._lock:
            self._refuse_while_restore_owed()
            # Every icon the message calls gets a picture file before the
            # message is rendered, because the render turns each call into that
            # file's label and cannot invent one. This is also what makes an
            # icon nobody has say so in the icon library's own words rather than
            # as "there is no picture holding it".
            claims = self._claim_pictures(message, replacing=key)

            # Rendered under the lock, because a message that calls a variable
            # renders to that variable's label, and a variable deleted between
            # the render and the write would leave the message calling a file
            # somebody else may be handed next.
            #
            # Whether a picture write was started, for the same reason as in
            # :meth:`rendering`: a message refused for its length or for a full
            # pool wrote nothing, and calling the sign out of sync for that
            # would report a fault on /health and redraw every picture at the
            # next check.
            drawing = False
            try:
                body = self._render_message(message)
                if len(body) > self._layout.slot_capacity:
                    raise MessageTooLong(
                        "the message renders to %d bytes but each slot holds %d. Shorten it, or "
                        "raise slot_capacity and restart, which reallocates the sign and clears it."
                        % (len(body), self._layout.slot_capacity)
                    )

                existed = key in self._state.slots
                previous = self._state.slots.get(key)
                # Raises LayoutFull when the pool is full.
                label = self._layout.slots.assign(key)
                # The pictures go to the sign before the message that calls
                # them, so the sign is never drawing a file that points at a
                # picture not yet written. Same rule as a variable, and for the
                # same reason.
                # Only when there is a picture to write. An empty claim list
                # is the common case, a message whose icons already have their
                # files included, and lifting an alert for it would take the
                # sign off an alert to write nothing.
                if claims.claimed:
                    async with self._priority_lifted():
                        drawing = True
                        await self._write_claims(claims.claimed)
            except BaseException:
                # The slot file has to go back too, and only here. A new key is
                # given one by ``assign`` a few lines up, and the record that
                # would own it is not built until after this block, so a failure
                # in between leaves a file reserved for a slot that never
                # existed. Nothing releases it afterwards: the key is in no
                # state, so no delete reaches it and no restart knows about it.
                # Enough failed writes and the pool reports itself full with
                # nothing in it, which is what this was measured doing.
                #
                # An existing key is the case to leave alone. It is already in
                # the state holding that file for the message that is still on
                # the sign, and giving it up would take the file from a slot the
                # sign is playing.
                if key not in self._state.slots:
                    self._layout.slots.release(key)
                self._undo_claims(claims, written=drawing)
                raise

            now = self._now()
            slot = SlotState(
                key=key,
                label=label.decode("ascii"),
                message=message,
                mode=mode,
                order=order,
                active=_resolve_active(active, previous),
                delete_on_expiry=delete_on_expiry,
                source=source,
                expires_at=now + timedelta(seconds=ttl_seconds) if ttl_seconds else None,
                updated_at=now,
            )

            self._state.slots[key] = slot
            # What the run sequence should say has changed, so it has to be
            # rewritten and not only the file. Two things decide that: whether
            # the slot is named at all, and where. ``list_slots`` sorts by
            # ``order`` before the key, so reordering an existing slot changes
            # the sequence without changing which slots are in it.
            sequence_changed = (
                previous is None
                or previous.active != slot.active
                or previous.order != slot.order
            )
            try:
                if previous is not None and previous.active and not slot.active:
                    # Going off the display, so stop the sign cycling to the
                    # file before touching it. The other way round, the write
                    # below would land in a file the sequence still names and
                    # restart the message a moment before it went.
                    await self._apply_run_sequence()
                    await self._hide(slot)
                else:
                    # Written before the sequence can name it, so the sign is
                    # never sent to a file that is still being filled. A hidden
                    # slot's new text goes to the sign too, into a file nothing
                    # is cycling to, which is what keeps switching it back on
                    # down to one write; :meth:`_hide` has the reasoning.
                    if slot.active:
                        await self._controller.write_text_file(
                            label, body, mode=mode_token.value
                        )
                    else:
                        await self._hide(slot)
                    if sequence_changed:
                        await self._apply_run_sequence()
            except BaseException:
                # The write did not land, so the slot is not on the sign, and
                # keeping it would promise what the sign is not showing. Put the
                # registry back exactly as it was, then let the error surface: a
                # sign that cannot be reached becomes the 503 that tells a client
                # it cannot write right now rather than a 200 that hides it. A new
                # message is theirs to retry when the link is back, and a failed
                # update leaves the previous one in place. The slots already on
                # the sign are re-pushed on reconnect regardless of this.
                #
                # The record going back does not put the sign back. These paths
                # can write twice, so the first can land and the second raise,
                # leaving the sign holding a sequence or a file the restored
                # record does not describe. Marking the registry dirty is what
                # makes ``GET /health`` report that and the next refresh repair
                # it. It costs a needless refresh when nothing landed at all,
                # which is the harmless way round.
                self._dirty = True
                if previous is not None:
                    self._state.slots[key] = previous
                else:
                    del self._state.slots[key]
                    self._layout.slots.release(key)
                # The pictures claimed for this message are given up too, since
                # nothing calls them now and leaving them claimed would hold
                # files against a message that is not on the sign.
                #
                # One of them may already be drawn on the sign, because the
                # pictures go first and it is the message write that failed.
                # That is left alone rather than blanked: the controller
                # remembers what each file holds, so the next icon given that
                # file writes over it, and an icon nobody calls sitting in a
                # file nothing draws from costs nothing until then.
                self._undo_claims(claims)
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
        for that file and declines to write them again. That write does disturb
        the display, measured on 2026-09-12, but far less than rewriting a TEXT
        file does: short enough to be imperceptible when anything else on screen
        is changing, and easy to miss even on static content. Writing the text
        as well would double the cost for nothing.

        The exception is the last one. A sign handed a run sequence naming
        nothing freezes on the message it was drawing and holds it indefinitely,
        measured on 2026-09-11 and recorded in docs/protocol-notes.md, so when
        no slot is left playing the file is emptied to break that. Switching one
        back on from there costs the text and the sequence, two writes, which is
        the price of coming back from a display that is holding nothing.
        """
        async with self._lock:
            self._refuse_while_restore_owed()
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
                except BaseException:
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
            self._refuse_while_restore_owed()
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
            self._refuse_while_restore_owed()
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
            if self._restore_owed:
                # Nothing can be written until the pool has, so expiries wait for
                # it rather than failing every sweep.
                return []
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
            self._refuse_while_restore_owed()
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
            except BaseException:
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
        """Delete a variable, refusing while any message, the alert or a waiting alert calls it."""
        async with self._lock:
            self._refuse_while_restore_owed()
            variable = self.get_variable(name)
            callers = self.callers(name)
            alert = self.alert_calls(name)
            waiting = self.waiting_alerts_calling(name)
            if callers or alert or waiting:
                raise VariableInUse(_in_use(name, callers, alert=alert, waiting=waiting))

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

    def _picture_labels(self) -> dict[tuple[str, str | None], bytes]:
        """Which picture file each icon lives in, for rendering the messages that call them."""
        return {
            (picture.name, picture.tint): picture.label.encode("latin-1")
            for picture in self._state.pictures.values()
        }

    # == picture files ======================================================

    def _icon_in_use(
        self, key: str, *, replacing: str | None = None, replacing_alert: bool = False
    ) -> bool:
        """Whether any slot or the alert still calls the icon this picture holds.

        Hidden slots count. A hidden slot is put back on the display by one run
        sequence write and no redraw, and that is only true while the icons its
        message calls still have their files. Evicting one would turn a free
        switch into a picture write, which blanks the display.

        ``replacing`` is the slot whose message is being replaced, and
        ``replacing_alert`` says the same of the alert. Neither is read, because
        what is recorded for them is the message on its way out: asking whether
        the old text still calls an icon would refuse a write whose result fits.
        A slot moving from ``<icon:sun>`` to ``<icon:moon>`` in a pool with one
        file is the whole of that case, and it has to be allowed.
        """
        wanted = (self._state.pictures[key].name, self._state.pictures[key].tint)
        for slot_key, slot in self._state.slots.items():
            if slot_key == replacing:
                continue
            if wanted in icon_references(slot.message):
                return True
        if replacing_alert:
            return False
        alert = self._state.alert
        return alert is not None and wanted in icon_references(alert.message)

    def _claim_picture(
        self,
        name: str,
        tint: str | None,
        *,
        keeping: set[str],
        replacing: str | None,
        replacing_alert: bool,
        evicted: list[PictureState],
    ) -> str | None:
        """Give one icon a picture file, returning its key if the file is newly claimed.

        None means the icon already had one, so nothing has to be written. The
        icon itself is resolved first, which is what turns a name nobody has
        into the library's own message rather than into "there is no picture
        holding it", and which is why this cannot be folded into the renderer.

        ``keeping`` is every icon the message being claimed for calls, its own
        included. Nothing in it may be evicted, or a message asking for more
        icons than the pool holds would take back a file it had just been given
        and then fail at the render, reporting a missing picture where the
        honest answer is a full pool.
        """
        icons.resolve(name, tint)  # raises IconError for a name or a tint we do not have
        if not self._layout.picture_count:
            raise IconsDisabled(
                "icons are switched off, because picture_count is 0. Raise it and "
                "restart, which reallocates the sign and clears it."
            )

        key = _picture_key(name, tint)
        if key in self._state.pictures:
            return None

        try:
            label = self._layout.pictures.assign(key)
        except LayoutFull:
            if not self._evict_a_picture(
                keeping=keeping,
                replacing=replacing,
                replacing_alert=replacing_alert,
                evicted=evicted,
            ):
                raise PicturePoolFull(
                    "all %d picture files are holding an icon that a message or the alert "
                    "still calls, so there is nowhere to draw <icon:%s>. Stop calling one, "
                    "or raise picture_count and restart, which reallocates the sign and "
                    "clears it." % (self._layout.picture_count, key)
                ) from None
            label = self._layout.pictures.assign(key)

        self._state.pictures[key] = PictureState(
            key=key, name=name, tint=tint, label=label.decode("latin-1")
        )
        return key

    def _evict_a_picture(
        self,
        *,
        keeping: set[str],
        replacing: str | None,
        replacing_alert: bool,
        evicted: list[PictureState],
    ) -> bool:
        """Give up one picture file nothing calls any more. Returns whether it found one.

        This is where the lazy release actually happens, and it is the only
        place a picture is given up. A file is kept after its last caller goes
        precisely so that the common case, a source alternating between two
        icons, costs nothing. It is only when a third icon turns up with nowhere
        to go that one of the idle ones pays.

        The idle picture given up is the one claimed longest ago, which is the
        order the record is kept in rather than a least recently used order.
        That is a choice and not an oversight: what the record does not hold is
        when each icon was last drawn, only when its file was claimed, and an
        icon nothing calls is not being drawn at all. Whichever idle file is
        taken costs the same one write when its icon is next called for.

        What it takes is appended to ``evicted``, so that a write which fails
        after this can put it back. Nothing in ``keeping`` is a candidate.
        """
        for key in list(self._state.pictures):
            if key in keeping:
                continue
            if not self._icon_in_use(
                key, replacing=replacing, replacing_alert=replacing_alert
            ):
                picture = self._state.pictures.pop(key)
                self._layout.pictures.release(key)
                evicted.append(picture)
                logger.info(
                    "picture file %s gave up icon %r, which nothing calls any more",
                    picture.label,
                    key,
                )
                return True
        return False

    def _claim_pictures(
        self, message: str, *, replacing: str | None = None, replacing_alert: bool = False
    ) -> _Claims:
        """Give every icon a message calls a picture file.

        The caller writes the claimed ones to the sign and, if anything after
        that fails, hands the whole record to :meth:`_undo_claims`.

        ``replacing`` names the slot this message is replacing, and
        ``replacing_alert`` says it is the alert. Whichever it is, its old
        message is left out of the question "does anything still call this
        icon", because the old message is the one going away.
        """
        wanted = icon_references(message)
        # Every icon this message calls, whether it already has a file or is
        # about to be given one. None of them may be evicted for one of the
        # others.
        keeping = {_picture_key(name, tint) for name, tint in wanted}
        claims = _Claims()
        try:
            for name, tint in wanted:
                key = self._claim_picture(
                    name,
                    tint,
                    keeping=keeping,
                    replacing=replacing,
                    replacing_alert=replacing_alert,
                    evicted=claims.evicted,
                )
                if key is not None:
                    claims.claimed.append(key)
        except BaseException:
            # Nothing has been drawn yet: the caller draws after this returns.
            self._undo_claims(claims, written=False)
            raise
        return claims

    def _undo_claims(self, claims: _Claims, *, written: bool = True) -> None:
        """Put the pool back as it was, for a message that did not reach the sign.

        The order is what makes it balance: giving up what was claimed frees
        exactly the files the evictions paid for, so what was evicted can be
        put back into them.

        A restored picture's file may hold the wrong bitmap, because the write
        that failed came after the eviction. That is the tolerance :meth:`upsert`
        relies on: the record says which icon belongs in which file, and the next
        refresh writes every one of them again. The alert service does not rely
        on it, because it writes the replaced alert straight back to the display
        and a wrong bitmap would be on screen rather than merely on record; it
        calls :meth:`_Rendering.revert`, which draws the evicted pictures again
        before it puts that alert back.

        ``written`` is False when no picture write was started after the claim,
        so every file still holds what the record is going back to and the sign
        is not out of step for it.
        """
        for key in claims.claimed:
            self._state.pictures.pop(key, None)
            self._layout.pictures.release(key)

        for picture in claims.evicted:
            try:
                self._layout.pictures.restore(picture.key, picture.label.encode("latin-1"))
            except ValueError:
                # The file is spoken for by something else now, which can only
                # happen if the pool moved under this. Losing the record is the
                # same outcome as never having put it back, and it must not
                # replace whatever failure brought us here.
                logger.info(
                    "could not give icon %r its file back after a failed write",
                    picture.key,
                )
                continue
            self._state.pictures[picture.key] = picture
            if written:
                self._dirty = True

    async def _write_picture(self, picture: PictureState) -> None:
        await self._controller.write_dots_file(
            picture.label.encode("latin-1"), icons.resolve(picture.name, picture.tint)
        )

    async def _write_claims(self, keys: list[str]) -> None:
        """Draw newly claimed icons into their files, before anything calls them.

        The sign has to be handed back for these, and it is not done here. This
        runs inside :meth:`rendering` as well, which the alert service calls
        with its own lock already held, so taking that lock here would wait on
        the caller. Every caller that is not the alert service wraps this in
        :meth:`_priority_lifted` instead, and the alert service hands the sign
        back itself.
        """
        for key in keys:
            await self._write_picture(self._state.pictures[key])

    @contextlib.asynccontextmanager
    async def _priority_lifted(self) -> AsyncIterator[Lift]:
        """Hand the sign back while pictures are written, if an alert is holding it.

        A picture written while a priority message is running is not taken by
        the sign, measured on 2026-09-13, and nothing retries it: the controller
        believes it sent those bytes and will not send them again until a
        refresh forgets. So the alert comes off for the write and goes straight
        back on.

        Wrapped around the picture writes and nothing else. The STRING, TEXT and
        run sequence writes that follow are all measured landing under an alert,
        so including them would lengthen the moment the alert is off the sign
        for no gain.

        Nothing happens when no alert service was wired in, which is the tests
        that use the registry on its own. Those get a lift that reports having
        put nothing back, which is true: with no alert service there is no
        priority file to put anything on.
        """
        hold = self._priority_hold
        if hold is None:
            yield _NoLift()
            return
        async with hold(self._render_message) as lift:
            yield lift

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
        return render(
            message,
            strict=strict,
            variables=self._variable_labels(),
            icons=self._picture_labels(),
        )

    def _check_message(self, message: str) -> bytes:
        """Render a message strictly as if every icon it calls had a picture file.

        Each icon is resolved, and refused with icons switched off, exactly as
        claiming it would; then it is rendered against a stand-in label, which
        costs the same two bytes as the label it will be given. Nothing is
        claimed, so the pool and the sign are untouched.

        A message calling more different icons than there are picture files is
        refused too, since claiming them would refuse it whatever else was on
        the sign. One that only finds the pool full now is not: the icons the
        slots are calling at its turn may be different ones, or fewer.
        """
        labels = self._picture_labels()
        wanted = icon_references(message)
        for name, tint in wanted:
            icons.resolve(name, tint)  # raises IconError for a name or a tint we do not have
            if not self._layout.picture_count:
                raise IconsDisabled(
                    "icons are switched off, because picture_count is 0. Raise it and "
                    "restart, which reallocates the sign and clears it."
                )
            labels.setdefault((name, tint), PICTURE_FILE_LABELS[0])
        distinct = {_picture_key(name, tint) for name, tint in wanted}
        if len(distinct) > self._layout.picture_count:
            # The one picture refusal that is certain. A pool full of icons
            # the slots call now may have room by the time this alert's turn
            # comes; a message calling more icons than there are files never
            # will, and claiming them would refuse it whatever else was on the
            # sign.
            raise PicturePoolFull(
                "this alert calls %d different icons and the sign has %d picture files, "
                "so it could never be drawn. Call fewer, or raise picture_count and "
                "restart, which reallocates the sign and clears it."
                % (len(distinct), self._layout.picture_count)
            )
        if references(message):
            self._require_variables()
        return render(message, strict=True, variables=self._variable_labels(), icons=labels)

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
        that file and declines to write them again. That write is not free, but
        it was measured on 2026-09-12 disturbing the display far less than a
        TEXT write does, briefly enough to be missed unless watched for.

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

    async def _apply_run_sequence(self) -> None:
        """Tell the sign which files to play.

        This goes out whatever else is on the sign, an alert included. The
        service used to hold these writes back while an alert was up, because
        the protocol says a running priority message is cancelled by a serial
        write to the run time table or the run day table and says nothing
        either way about the run sequence. The spike settled it on 2026-09-11:
        with an alert holding the whole display the sequence was rewritten from
        A B C to A B, a real change rather than a no-op, and the alert stayed
        up. A Set Run Sequence write is not a fifth thing that cancels one, so
        there is nothing here to protect against.
        """
        # Only the active ones. An inactive slot keeps its file and its place in
        # the order and is simply not named here, which is the whole mechanism
        # behind switching a message off.
        labels = [slot.label.encode("ascii") for slot in self.list_slots() if slot.active]
        await self._controller.set_run_sequence(labels)

    def _save(self) -> None:
        self._store.save(self._state)

    def _save_between_the_pool_and_the_rewrite(self) -> None:
        """Save that the pool went out, without letting a failure stop the rewrite.

        Only for the moment after a memory configuration has been written and
        before every file is put back. The sign is blank there, and the rewrite
        is what brings it back, so a state file that cannot be written is
        logged rather than raised. What the file then still says is that the
        pool is pending, which is the safe thing for it to say. The save after
        the rewrite usually clears it; only if that one fails too does the next
        start write the pool again and put everything back.
        """
        try:
            self._save()
        except OSError as err:
            logger.warning(
                "could not record that the sign's file pool was written; unless a "
                "later save succeeds, the next start will write it again: %s",
                err,
            )


def _resolve_active(asked: bool | None, previous: SlotState | None) -> bool:
    """Work out whether a slot should be showing after an upsert.

    Saying nothing is the interesting case, and it is the common one: a source
    pushing the same message on a timer sends no opinion about whether it is
    showing, and must not be able to undo a deliberate hiding by repeating
    itself. So None means whatever it already was, and a slot nobody has an
    opinion about yet is showing.
    """
    if asked is not None:
        return asked
    return previous.active if previous is not None else True


def _in_use(
    name: str, callers: list[str], *, alert: bool, waiting: list[str | None] | None = None
) -> str:
    """Say what still calls a variable, and what to do about each.

    ``waiting`` is the keys of the waiting alerts that call it, None for one
    with no key, which can only be cancelled by emptying the whole queue.
    """
    waiting = waiting or []
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
    if waiting:
        keys = [key for key in waiting if key is not None]
        holders.append(
            "an alert waiting its turn" if len(waiting) == 1 else "%d alerts waiting their turn"
            % len(waiting)
        )
        if keys:
            remedies.append(
                "cancel %s with %s"
                % (
                    "it" if len(waiting) == 1 else "those with a key",
                    ", ".join("DELETE /alerts/queue/%s" % key for key in keys),
                )
            )
        if len(keys) < len(waiting):
            remedies.append(
                "empty the queue with DELETE /alerts/queue, since an alert sent without a "
                "key cannot be cancelled on its own"
            )
    return (
        "variable %r is called by %s. First %s; deleting it now would leave %s calling a file "
        "another variable could be given next."
        % (
            name,
            " and ".join(holders),
            " and ".join(remedies),
            "it" if len(callers) + alert + len(waiting) == 1 else "them",
        )
    )


__all__ = [
    "LayoutFull",
    "MessageTooLong",
    "RegistryError",
    "SlotRegistry",
    "UnknownSlot",
    "UnknownVariable",
    "VariableInUse",
    "VariableTooLong",
    "VariablesDisabled",
]
