"""What the service remembers across a restart.

Three things have to survive: which messages are registered and where on the
sign each one lives, whether an alert is currently holding the display, and
which memory configuration was last applied.

The last of those matters more than it looks. Writing a memory configuration
erases every file on the sign, so the service must be able to tell "the pool I
want is the pool that is already there" from "the pool changed", and only
reconfigure in the second case. Without this record, every restart would wipe
the sign.

Writes are atomic. A half-written state file after a power cut would strand the
sign, and a Pi losing power is exactly the event this service is expected to
recover from.

The rename that makes a write atomic is retried a few times, for Windows, where
another process holding a handle on a file for an instant is enough to make the
rename fail rather than wait.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

STATE_VERSION = 1


class SlotState(BaseModel):
    """One registered message."""

    key: str
    label: str
    message: str
    mode: str
    position: str
    order: int = 0
    source: str | None = None
    expires_at: datetime | None = None
    updated_at: datetime


class AlertState(BaseModel):
    """An alert holding the priority file."""

    message: str
    mode: str
    position: str
    started_at: datetime
    expires_at: datetime | None = None


class AppliedLayout(BaseModel):
    """The memory configuration currently believed to be on the sign."""

    slot_count: int
    slot_capacity: int
    labels: list[str]

    def matches(self, slot_count: int, slot_capacity: int) -> bool:
        """Whether this layout is already what the given settings ask for."""
        return self.slot_count == slot_count and self.slot_capacity == slot_capacity


class ServiceState(BaseModel):
    """Everything persisted, in one document."""

    version: int = STATE_VERSION
    slots: dict[str, SlotState] = Field(default_factory=dict)
    alert: AlertState | None = None
    layout: AppliedLayout | None = None


REPLACE_ATTEMPTS = 5
REPLACE_RETRY_DELAY = 0.01


def _replace_with_retries(temporary: Path, target: Path) -> None:
    """Rename over the target, retrying the brief lock Windows can impose.

    Windows refuses a rename while any handle is open on either file, and a
    virus scanner opens a file the moment it is written, so a save that is
    correct on POSIX fails there at random. Measured on one Windows machine
    with Defender running, replacing a freshly written file failed about once
    in thirty five attempts, and a single immediate retry cleared every one of
    them, because the handle is gone within microseconds.

    So the second attempt is immediate, and only the ones after it wait. That
    matters because this is called from coroutines: a sleep here stops the
    event loop, and the common case is over before one would have started.

    POSIX renames over an open file without complaint, so nothing there ever
    reaches the second attempt.
    """
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, target)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            if attempt > 0:
                time.sleep(REPLACE_RETRY_DELAY)


class StateStore:
    """Loads and saves :class:`ServiceState` as JSON, atomically."""

    def __init__(self, path: Path) -> None:
        """Point the store at a file. The file need not exist yet."""
        self.path = path
        self._last_written: str | None = None
        self._leftovers: list[Path] = []
        self.writes = 0
        self.skipped = 0

    def _discard(self, temporary: Path) -> None:
        """Remove a temporary file the save will not use, without masking why.

        A rename that spent every attempt did so because a handle stayed open,
        and Windows refuses to delete a file in that state as readily as it
        refuses to rename it. Raising from here would replace the failure that
        matters with a second one describing the tidying up, so the file is
        logged and remembered instead, and the next save tries again.
        """
        try:
            temporary.unlink(missing_ok=True)
        except OSError as err:
            logger.warning("could not remove the temporary file %s (%s)", temporary, err)
            self._leftovers.append(temporary)

    def _sweep_leftovers(self) -> None:
        """Try again to remove the temporary files earlier saves gave up on.

        Without this, a state directory something else holds open would collect
        one file per failed save, and since everything is re-pushed on a timer
        that is one per cycle for as long as the lock lasts. Only files this
        store has already finished with are touched, so a save in flight
        elsewhere is never disturbed.

        This runs ahead of the identical-payload check in :meth:`save`, so a
        save that writes nothing still tidies up after one that failed.
        """
        remaining = []
        for leftover in self._leftovers:
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                remaining.append(leftover)
        self._leftovers = remaining

    def _sweep_orphans(self) -> None:
        """Remove temporary files an earlier run of the service left behind.

        A store only remembers its own leftovers, so a process that exits while
        the directory is still locked leaves them for the next one. This runs
        from :meth:`load`, which happens once at startup, so nothing it deletes
        can belong to a save that is under way. It assumes one service owns the
        state file, which is the same thing the sign itself assumes.
        """
        prefix = self.path.name + "."
        try:
            entries = list(self.path.parent.iterdir())
        except OSError:
            return

        for entry in entries:
            if not (entry.name.startswith(prefix) and entry.name.endswith(".tmp")):
                continue
            try:
                entry.unlink()
            except OSError as err:
                logger.warning("could not remove the leftover file %s (%s)", entry, err)

    def load(self) -> ServiceState:
        """Read the state, returning a fresh one if there is nothing usable to read.

        A corrupt or unreadable state file is a bad reason to refuse to start.
        The sign can be repopulated by its sources; refusing to boot cannot be
        fixed without someone logging in. So it is logged loudly and set aside.
        """
        self._sweep_orphans()

        if not self.path.exists():
            logger.info("no state file at %s; starting empty", self.path)
            return ServiceState()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            logger.error("could not read state from %s (%s); starting empty", self.path, err)
            return ServiceState()

        version = raw.get("version")
        if version != STATE_VERSION:
            logger.error(
                "state at %s is version %r, not %d; starting empty",
                self.path,
                version,
                STATE_VERSION,
            )
            return ServiceState()

        try:
            state = ServiceState.model_validate(raw)
        except ValueError as err:
            logger.error("state at %s did not validate (%s); starting empty", self.path, err)
            return ServiceState()

        logger.info(
            "restored %d slot(s)%s from %s",
            len(state.slots),
            " and an active alert" if state.alert else "",
            self.path,
        )
        return state

    def save(self, state: ServiceState) -> None:
        """Write the state out, replacing the old file in one step.

        A save whose content is identical to the last one written is skipped.
        This runs on a Raspberry Pi with an SD card underneath it, and the
        service saves on every operation whether or not the operation changed
        anything: sweeps that expire nothing, restores, releases with no alert
        to release.

        An upsert that genuinely re-sends the same temperature is not one of
        those, because ``updated_at`` moves. That is deliberate. Knowing when a
        source last wrote is how a dead automation becomes visible, and it is
        worth one small write per message to keep.

        The rename at the end is retried; see :func:`_replace_with_retries`.
        """
        self._sweep_leftovers()

        payload = state.model_dump_json(indent=2)
        if payload == self._last_written and self.path.exists():
            self.skipped += 1
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)

        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed, then renamed
            mode="w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=self.path.name + ".",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        try:
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retries(temporary, self.path)
        except OSError:
            self._discard(temporary)
            raise

        self._last_written = payload
        self.writes += 1
