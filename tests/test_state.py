"""Tests for the persisted state and the file pool layout."""

import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from readerboard.sign.layout import Layout, LayoutFull
from readerboard.sign.state import (
    REPLACE_ATTEMPTS,
    STATE_VERSION,
    AppliedLayout,
    ServiceState,
    SlotState,
    StateStore,
)


def a_slot(key: str = "temperature", label: str = "A") -> SlotState:
    return SlotState(
        key=key,
        label=label,
        message="<green>18.4<degree>",
        mode="HOLD",
        position="MIDDLE",
        updated_at=datetime(2026, 8, 25, 12, 0, tzinfo=UTC),
    )


class TestStateStore:
    def test_a_missing_file_gives_an_empty_state(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        state = store.load()
        assert state.slots == {}
        assert state.alert is None
        assert state.layout is None

    def test_a_round_trip_preserves_the_slots(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        store.save(ServiceState(slots={"temperature": a_slot()}))

        restored = store.load()
        assert restored.slots["temperature"].message == "<green>18.4<degree>"
        assert restored.slots["temperature"].label == "A"

    def test_it_creates_the_directory_it_needs(self, tmp_path):
        store = StateStore(tmp_path / "deeper" / "still" / "state.json")
        store.save(ServiceState())
        assert store.path.exists()

    def test_saving_leaves_no_temporary_files_behind(self, tmp_path):
        store = StateStore(tmp_path / "state.json")
        store.save(ServiceState(slots={"temperature": a_slot()}))
        store.save(ServiceState(slots={"temperature": a_slot()}))
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_rename_windows_briefly_refuses_is_retried(self, tmp_path, monkeypatch):
        renames = []
        real_replace = os.replace

        def refuses_the_first_one(source, target):
            renames.append(source)
            if len(renames) == 1:
                raise PermissionError(5, "Access is denied")
            real_replace(source, target)

        monkeypatch.setattr(os, "replace", refuses_the_first_one)
        store = StateStore(tmp_path / "state.json")
        store.save(ServiceState(slots={"temperature": a_slot()}))

        assert len(renames) == 2
        assert store.load().slots["temperature"].message == "<green>18.4<degree>"
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_the_first_retry_does_not_wait(self, tmp_path, monkeypatch):
        renames = []
        waits = []
        real_replace = os.replace

        def refuses_the_first_one(source, target):
            renames.append(source)
            if len(renames) == 1:
                raise PermissionError(5, "Access is denied")
            real_replace(source, target)

        monkeypatch.setattr(os, "replace", refuses_the_first_one)
        monkeypatch.setattr(time, "sleep", waits.append)
        StateStore(tmp_path / "state.json").save(ServiceState(slots={"temperature": a_slot()}))

        assert len(renames) == 2
        assert waits == []

    def test_a_rename_that_never_succeeds_leaves_the_last_good_state(self, tmp_path, monkeypatch):
        store = StateStore(tmp_path / "state.json")
        store.save(ServiceState(slots={"temperature": a_slot()}))
        as_written = store.path.read_text(encoding="utf-8")

        renames = []

        def refuses_every_one(source, target):
            renames.append(source)
            raise PermissionError(5, "Access is denied")

        monkeypatch.setattr(os, "replace", refuses_every_one)
        with pytest.raises(PermissionError):
            store.save(ServiceState(slots={"humidity": a_slot(key="humidity", label="B")}))

        assert len(renames) == REPLACE_ATTEMPTS
        assert store.path.read_text(encoding="utf-8") == as_written
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_temporary_file_that_cannot_be_removed_does_not_hide_the_real_error(
        self, tmp_path, monkeypatch, caplog
    ):
        store = StateStore(tmp_path / "state.json")

        def refuses_every_one(source, target):
            raise PermissionError(5, "Access is denied")

        def refuses_to_delete(self, missing_ok=False):
            raise PermissionError(13, "The process cannot access the file")

        monkeypatch.setattr(os, "replace", refuses_every_one)
        monkeypatch.setattr(Path, "unlink", refuses_to_delete)
        with pytest.raises(PermissionError) as raised:
            store.save(ServiceState(slots={"temperature": a_slot()}))

        assert "Access is denied" in str(raised.value)
        assert "could not remove the temporary file" in caplog.text

    @pytest.mark.skipif(
        os.name != "nt", reason="only Windows refuses to rename or delete a file held open"
    )
    def test_a_real_lock_behaves_as_the_test_above_pretends(self, tmp_path, monkeypatch, caplog):
        # The test above mocks both the rename and the delete. This one mocks
        # neither: it holds an ordinary read handle on the temporary file, which
        # is enough to make Windows refuse both, so the path runs for real.
        store = StateStore(tmp_path / "state.json")
        store.save(ServiceState(slots={"temperature": a_slot()}))
        as_written = store.path.read_text(encoding="utf-8")

        holders = []
        a_temporary_file = tempfile.NamedTemporaryFile

        def held_open(*args, **kwargs):
            handle = a_temporary_file(*args, **kwargs)
            # The handle has to outlive this call, so no context manager.
            holders.append(open(handle.name, encoding="utf-8"))  # noqa: SIM115
            return handle

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", held_open)
        try:
            with pytest.raises(PermissionError):
                store.save(ServiceState(slots={"humidity": a_slot(key="humidity", label="B")}))
        finally:
            for holder in holders:
                holder.close()

        assert store.path.read_text(encoding="utf-8") == as_written
        assert "could not remove the temporary file" in caplog.text
        assert sorted(p.suffix for p in tmp_path.iterdir()) == [".json", ".tmp"]

    def test_a_temporary_file_left_behind_is_removed_by_the_next_save(self, tmp_path, monkeypatch):
        store = StateStore(tmp_path / "state.json")

        def refuses_every_one(source, target):
            raise PermissionError(5, "Access is denied")

        def refuses_to_delete(self, missing_ok=False):
            raise PermissionError(13, "The process cannot access the file")

        monkeypatch.setattr(os, "replace", refuses_every_one)
        monkeypatch.setattr(Path, "unlink", refuses_to_delete)
        with pytest.raises(PermissionError):
            store.save(ServiceState(slots={"temperature": a_slot()}))

        assert [p.suffix for p in tmp_path.iterdir()] == [".tmp"]

        monkeypatch.undo()
        store.save(ServiceState(slots={"temperature": a_slot()}))

        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_save_that_writes_nothing_still_removes_a_leftover(self, tmp_path, monkeypatch):
        store = StateStore(tmp_path / "state.json")
        store.save(ServiceState(slots={"temperature": a_slot()}))

        def refuses_every_one(source, target):
            raise PermissionError(5, "Access is denied")

        def refuses_to_delete(self, missing_ok=False):
            raise PermissionError(13, "The process cannot access the file")

        monkeypatch.setattr(os, "replace", refuses_every_one)
        monkeypatch.setattr(Path, "unlink", refuses_to_delete)
        with pytest.raises(PermissionError):
            store.save(ServiceState(slots={"humidity": a_slot(key="humidity", label="B")}))

        # The save below is the one that failed above undone: its payload is
        # what was last written, so it is skipped and writes nothing at all.
        monkeypatch.undo()
        store.save(ServiceState(slots={"temperature": a_slot()}))

        assert store.skipped == 1
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_a_temporary_file_from_an_earlier_run_is_removed_at_startup(self, tmp_path):
        path = tmp_path / "state.json"
        StateStore(path).save(ServiceState(slots={"temperature": a_slot()}))
        (tmp_path / "state.json.abandoned.tmp").write_text("{}", encoding="utf-8")

        state = StateStore(path).load()

        assert state.slots["temperature"].label == "A"
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_corrupt_json_does_not_stop_the_service_starting(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        path.write_text("{ this is not json", encoding="utf-8")

        state = StateStore(path).load()

        assert state.slots == {}
        assert "could not read state" in caplog.text

    def test_a_state_from_a_future_version_is_set_aside(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        path.write_text('{"version": 999, "slots": {}}', encoding="utf-8")

        state = StateStore(path).load()

        assert state.slots == {}
        assert "not %d" % STATE_VERSION in caplog.text or "999" in caplog.text

    def test_state_that_does_not_validate_is_set_aside(self, tmp_path, caplog):
        path = tmp_path / "state.json"
        path.write_text(
            '{"version": %d, "slots": {"a": {"key": "a"}}}' % STATE_VERSION,
            encoding="utf-8",
        )

        state = StateStore(path).load()

        assert state.slots == {}
        assert "did not validate" in caplog.text


class TestAppliedLayout:
    def test_it_matches_the_same_shape(self):
        applied = AppliedLayout(slot_count=4, slot_capacity=256, labels=list("ABCD"))
        assert applied.matches(4, 256)

    def test_a_different_count_does_not_match(self):
        applied = AppliedLayout(slot_count=4, slot_capacity=256, labels=list("ABCD"))
        assert not applied.matches(5, 256)

    def test_a_different_capacity_does_not_match(self):
        applied = AppliedLayout(slot_count=4, slot_capacity=256, labels=list("ABCD"))
        assert not applied.matches(4, 512)


class TestLayout:
    def test_the_pool_starts_at_a(self):
        assert Layout(3, 256).labels == (b"A", b"B", b"C")

    def test_an_impossible_pool_is_rejected(self):
        with pytest.raises(ValueError, match="between 1 and 26"):
            Layout(27, 256)

    def test_allocations_describe_every_file(self):
        allocations = Layout(2, 256).allocations()
        assert [entry.label for entry in allocations] == [b"A", b"B"]
        assert all(entry.capacity == 256 for entry in allocations)

    def test_assigning_is_stable_for_the_same_key(self):
        layout = Layout(3, 256)
        assert layout.assign("temperature") == b"A"
        assert layout.assign("temperature") == b"A"

    def test_different_keys_get_different_files(self):
        layout = Layout(3, 256)
        assert layout.assign("one") == b"A"
        assert layout.assign("two") == b"B"

    def test_a_released_file_is_handed_out_again(self):
        layout = Layout(2, 256)
        layout.assign("one")
        layout.assign("two")
        assert layout.release("one") == b"A"
        assert layout.assign("three") == b"A"

    def test_releasing_something_that_was_never_assigned_is_harmless(self):
        assert Layout(2, 256).release("nobody") is None

    def test_a_full_pool_refuses_rather_than_dropping_a_message(self):
        layout = Layout(2, 256)
        layout.assign("one")
        layout.assign("two")
        with pytest.raises(LayoutFull, match="all 2 message slots"):
            layout.assign("three")

    def test_free_count_tracks_assignments(self):
        layout = Layout(3, 256)
        assert layout.free_count == 3
        layout.assign("one")
        assert layout.free_count == 2

    def test_restoring_an_assignment_from_the_state_file(self):
        layout = Layout(3, 256)
        layout.restore("temperature", b"C")
        assert layout.label_for("temperature") == b"C"
        # The restored file must not then be handed to somebody else.
        assert layout.assign("other") == b"A"

    def test_restoring_a_file_outside_a_shrunken_pool_is_refused(self):
        # slot_count was lowered between runs, so file D no longer exists.
        layout = Layout(3, 256)
        with pytest.raises(ValueError, match="outside the pool"):
            layout.restore("temperature", b"D")


class TestNeedsReconfiguration:
    def test_a_sign_that_was_never_configured_needs_it(self):
        assert Layout(4, 256).needs_reconfiguration(None)

    def test_an_unchanged_pool_does_not(self):
        layout = Layout(4, 256)
        assert not layout.needs_reconfiguration(layout.as_applied())

    def test_a_changed_pool_does(self):
        applied = Layout(4, 256).as_applied()
        assert Layout(5, 256).needs_reconfiguration(applied)
        assert Layout(4, 512).needs_reconfiguration(applied)
