"""Each note fires when the sign would have done the thing quietly.

Every rule checked here is quoted in ``docs/protocol-notes.md``. If one of these
starts failing, either the model drifted or the document was read wrong, and it
is worth finding out which before changing the test.
"""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from signsim import decode
from signsim.framing import FrameScanner
from signsim.model import NoteLevel, SignState


@pytest.fixture
def sign():
    return SignState()


def send(sign, built):
    """Apply one payload to the sign and return the notes it produced."""
    found = FrameScanner().feed(frames.packet(built))
    assert len(found) == 1
    return sign.apply(decode.decode(found[0]).command)


def texts(notes):
    """Just the note text, for the substring assertions below."""
    return " | ".join(note.text for note in notes)


def configure(sign, *allocations):
    """Give the sign a memory configuration and return the notes."""
    return send(sign, frames.set_memory_config(list(allocations)))


class TestWritingBeforeConfiguration:
    def test_a_write_to_an_unconfigured_file_is_refused(self, sign):
        notes = send(sign, frames.write_text_file(b"B", b"TOO SOON"))
        assert any(note.level is NoteLevel.VIOLATION for note in notes)
        assert "before any memory configuration" in texts(notes)
        assert sign.files == {}

    def test_the_default_file_a_may_be_written_first(self, sign):
        send(sign, frames.write_text_file(b"A", b"ALLOWED"))
        assert sign.files[b"A"].body == b"ALLOWED"

    def test_the_priority_file_may_be_written_first(self, sign):
        send(sign, frames.write_text_file(c.FILE_PRIORITY, b"ALERT"))
        assert sign.priority == b"ALERT"

    def test_a_file_outside_the_configuration_is_refused(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        notes = send(sign, frames.write_text_file(b"D", b"NOWHERE"))
        assert "not in the sign's memory configuration" in texts(notes)
        assert b"D" not in sign.files


class TestMemoryConfiguration:
    def test_configuring_erases_the_messages_already_there(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        send(sign, frames.write_text_file(b"A", b"KEEP ME"))
        send(sign, frames.write_text_file(b"B", b"ME TOO"))
        notes = configure(sign, frames.FileAllocation(b"A", 128))
        assert "overwrote the previous table" in texts(notes)
        assert sign.files == {}

    def test_the_first_file_is_called_out_as_getting_the_rest_of_the_pool(self, sign):
        notes = configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        assert "whatever is left of the memory pool" in texts(notes)

    def test_a_duplicated_label_is_a_violation(self, sign):
        built = c.COMMAND_WRITE_SPECIAL + c.SF_SET_MEMORY_CONFIG + b"AAU0040FFFF" b"AAU0040FFFF"
        notes = send(sign, built)
        assert any(note.level is NoteLevel.VIOLATION for note in notes)
        assert "appears twice" in texts(notes)
        assert list(sign.memory_config) == [b"A"]

    def test_the_claimed_total_counts_the_per_file_overhead(self, sign):
        configure(sign, frames.FileAllocation(b"A", 100), frames.FileAllocation(b"B", 100))
        assert sign.memory_claimed == 2 * (100 + c.FILE_OVERHEAD_BYTES)

    def test_clearing_memory_leaves_no_table_at_all(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.write_text_file(b"A", b"HI"))
        notes = send(sign, frames.clear_memory())
        assert "Memory cleared" in texts(notes)
        assert sign.memory_config is None
        assert sign.files == {}

    def test_reconfiguring_notices_a_run_sequence_left_dangling(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        send(sign, frames.set_run_sequence([b"A", b"B"]))
        notes = configure(sign, frames.FileAllocation(b"A", 64))
        assert "still names B" in texts(notes)


class TestCapacity:
    def test_a_message_longer_than_its_file_is_truncated(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 16))
        notes = send(sign, frames.write_text_file(b"B", b"X" * 40))
        assert any(note.level is NoteLevel.WARNING for note in notes)
        assert "the rest never appears" in texts(notes)
        assert sign.files[b"B"].truncated_to == 16

    def test_a_truncated_file_reads_back_as_what_the_sign_kept(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 16))
        send(sign, frames.write_text_file(b"B", b"X" * 40))
        stored = sign.files[b"B"]
        assert stored.body == b"X" * 40
        assert stored.visible == b"X" * 16
        assert stored.rendered == "X" * 16

    def test_the_first_file_is_not_warned_about(self, sign):
        # The sign gives the first configured file whatever is left of the pool,
        # so its configured size is not the size it ends up with and a length
        # check against it would be a warning about nothing.
        configure(sign, frames.FileAllocation(b"A", 16), frames.FileAllocation(b"B", 16))
        send(sign, frames.set_run_sequence([b"A"]))
        notes = send(sign, frames.write_text_file(b"A", b"X" * 40))
        assert "the rest never appears" not in texts(notes)
        assert "It probably fits" in texts(notes)
        assert sign.files[b"A"].truncated_to is None

    def test_a_message_that_fits_draws_no_capacity_note(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        send(sign, frames.set_run_sequence([b"B"]))
        notes = send(sign, frames.write_text_file(b"B", b"SHORT"))
        assert notes == []


class TestPriority:
    def test_a_priority_write_suppresses_everything_else(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.set_run_sequence([b"A"]))
        send(sign, frames.write_text_file(b"A", b"ROTATING"))
        notes = send(sign, frames.write_text_file(c.FILE_PRIORITY, b"ALERT"))
        assert "stops being displayed" in texts(notes)
        assert sign.priority_active
        assert sign.playing == [c.FILE_PRIORITY]

    def test_an_empty_priority_write_hands_the_sign_back(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.set_run_sequence([b"A"]))
        send(sign, frames.write_text_file(b"A", b"ROTATING"))
        send(sign, frames.write_text_file(c.FILE_PRIORITY, b"ALERT"))
        notes = send(sign, frames.clear_priority_file())
        assert "resumes its run sequence" in texts(notes)
        assert not sign.priority_active
        assert sign.playing == [b"A"]

    def test_a_priority_message_over_the_fixed_capacity_loses_its_end(self, sign):
        oversize = b"Y" * (c.PRIORITY_FILE_CAPACITY + 5)
        notes = send(sign, c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY + c.SOM + b" b" + oversize)
        assert "the end of it is lost" in texts(notes)
        assert len(sign.priority) == c.PRIORITY_FILE_CAPACITY

    def test_a_write_during_an_alert_lands_but_shows_nothing(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.set_run_sequence([b"A"]))
        send(sign, frames.write_text_file(c.FILE_PRIORITY, b"ALERT"))
        notes = send(sign, frames.write_text_file(b"A", b"BEHIND THE ALERT"))
        assert "nothing of it shows until the priority file is released" in texts(notes)
        assert sign.files[b"A"].body == b"BEHIND THE ALERT"

    def test_a_run_sequence_during_an_alert_is_flagged_as_the_open_question(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.write_text_file(c.FILE_PRIORITY, b"ALERT"))
        notes = send(sign, frames.set_run_sequence([b"A"]))
        assert "does not say either way" in texts(notes)


class TestRunSequence:
    def test_a_label_that_is_not_configured_is_skipped(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        notes = send(sign, frames.set_run_sequence([b"A", b"Z"]))
        assert "processes the next label" in texts(notes)
        assert sign.playing == [b"A"]

    def test_a_configured_but_empty_file_is_mentioned(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        notes = send(sign, frames.set_run_sequence([b"B"]))
        assert "hold no message yet" in texts(notes)

    def test_an_empty_sequence_means_the_sign_plays_nothing(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        notes = send(sign, frames.set_run_sequence([]))
        assert "plays nothing from the pool" in texts(notes)
        assert sign.playing == []

    def test_a_file_written_but_not_yet_sequenced_is_mentioned(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        notes = send(sign, frames.write_text_file(b"B", b"ORPHAN"))
        assert "the run sequence does not name yet" in texts(notes)
        # Not a warning. The service writes the file and then rewrites the
        # sequence, so this is true of nearly every write for one transmission,
        # and colouring those rows as problems would hide the real ones.
        assert all(note.level is NoteLevel.INFO for note in notes)

    def test_blanking_a_file_the_sequence_does_not_name_says_nothing(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64), frames.FileAllocation(b"B", 64))
        assert send(sign, frames.write_text_file(b"B", b"")) == []

    def test_a_duplicated_label_is_a_violation(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        notes = send(sign, c.COMMAND_WRITE_SPECIAL + c.SF_SET_RUN_SEQUENCE + b"SUAA")
        assert any(note.level is NoteLevel.VIOLATION for note in notes)
        assert "named twice" in texts(notes)


class TestClock:
    def test_the_time_is_remembered(self, sign):
        send(sign, frames.set_time(14, 30))
        assert (sign.hour, sign.minute) == (14, 30)

    def test_the_day_is_named_in_the_note(self, sign):
        notes = send(sign, frames.set_day_of_week(1))
        assert "Sunday" in texts(notes)
        assert sign.day == 1

    def test_the_time_format_is_remembered(self, sign):
        send(sign, frames.set_time_format(True))
        assert sign.military_time is True


class TestState:
    def test_a_stored_file_reads_back_as_the_markup_that_made_it(self, sign):
        from readerboard.protocol.markup import render

        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.write_text_file(b"A", render("<red>HI<degree>")))
        assert sign.files[b"A"].rendered == "<red>HI<degree>"

    def test_transmissions_are_counted(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.write_text_file(b"A", b"HI"))
        assert sign.transmissions == 2

    def test_reset_puts_the_sign_back_to_switch_on(self, sign):
        configure(sign, frames.FileAllocation(b"A", 64))
        send(sign, frames.write_text_file(b"A", b"HI"))
        send(sign, frames.set_run_sequence([b"A"]))
        sign.reset()
        assert sign.memory_config is None
        assert sign.files == {}
        assert sign.run_sequence == []
        assert sign.transmissions == 0
