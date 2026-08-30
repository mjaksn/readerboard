"""Whatever the service builds, the simulator has to read back as what it was.

The round trip below is the test that makes this tool worth trusting. It takes
the frame builders the service actually sends with, pushes their output through
the scanner and the decoder, and checks that what comes out the far end is the
command that went in, with the same file labels, the same mode and the same
body. A decoder that guesses would pass one of these and fail the rest.

It has one limit, and it is the same limit stated at the top of ``spans.py``:
both sides read ``readerboard.protocol.constants``, so this proves the decoder
agrees with the encoder, never that either agrees with the protocol document.
``tests/test_constant_values.py`` in the service is what checks that.
"""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import render
from signsim import decode
from signsim.framing import FrameScanner


def decoded(packet):
    """Push a whole packet through the scanner and decode the one frame in it."""
    found = FrameScanner().feed(packet)
    assert len(found) == 1
    return decode.decode(found[0])


def payload(built):
    """Wrap a payload the way the service does and decode it."""
    return decoded(frames.packet(built))


class TestRoundTrip:
    def test_a_write_of_rendered_markup(self):
        body = render("<red>HELLO <degree>")
        result = payload(frames.write_text_file(b"C", body, mode=c.MODE_ROTATE))
        command = result.command
        assert isinstance(command, decode.WriteText)
        assert command.label == b"C"
        assert command.body == body
        assert command.mode == c.MODE_ROTATE
        assert command.position == c.TEXT_POS_MIDDLE
        assert result.complaints == ()
        assert command.summary == 'Write TEXT file C: <red>HELLO <degree>'

    def test_a_two_byte_display_mode_is_read_whole(self):
        result = payload(frames.write_text_file(b"A", b"HI", mode=c.MODE_STARBURST))
        assert result.command.mode == c.MODE_STARBURST
        assert result.command.body == b"HI"

    def test_a_memory_configuration(self):
        allocations = [frames.FileAllocation(b"A", 256), frames.FileAllocation(b"B", 512)]
        result = payload(frames.set_memory_config(allocations))
        command = result.command
        assert isinstance(command, decode.SetMemoryConfig)
        assert [one.label for one in command.entries] == [b"A", b"B"]
        assert [one.capacity for one in command.entries] == [256, 512]
        assert all(one.file_type == c.FILE_TYPE_TEXT for one in command.entries)
        assert all(not one.locked for one in command.entries)
        assert all(one.always_eligible for one in command.entries)
        assert result.complaints == ()

    def test_clear_memory_is_not_a_memory_configuration(self):
        assert isinstance(payload(frames.clear_memory()).command, decode.ClearMemory)

    def test_a_run_sequence(self):
        result = payload(frames.set_run_sequence([b"A", b"B", b"C"]))
        command = result.command
        assert isinstance(command, decode.SetRunSequence)
        assert command.labels == (b"A", b"B", b"C")
        assert command.sequence_mode == c.RUN_SEQ_IGNORE_TIME
        assert not command.locked
        assert result.complaints == ()

    def test_an_empty_run_sequence(self):
        command = payload(frames.set_run_sequence([])).command
        assert isinstance(command, decode.SetRunSequence)
        assert command.labels == ()

    def test_releasing_the_priority_file(self):
        command = payload(frames.clear_priority_file()).command
        assert isinstance(command, decode.WriteText)
        assert command.label == c.FILE_PRIORITY
        assert command.body == b""
        assert "Release the priority file" in command.summary

    @pytest.mark.parametrize(("hour", "minute"), [(0, 0), (9, 5), (23, 59)])
    def test_setting_the_clock(self, hour, minute):
        result = payload(frames.set_time(hour, minute))
        assert isinstance(result.command, decode.SetTime)
        assert (result.command.hour, result.command.minute) == (hour, minute)
        assert result.complaints == ()

    @pytest.mark.parametrize("day", range(1, 8))
    def test_setting_the_day_of_week(self, day):
        result = payload(frames.set_day_of_week(day))
        assert isinstance(result.command, decode.SetDayOfWeek)
        assert result.command.day == day
        assert result.complaints == ()

    @pytest.mark.parametrize("military", [True, False])
    def test_setting_the_time_format(self, military):
        result = payload(frames.set_time_format(military))
        assert isinstance(result.command, decode.SetTimeFormat)
        assert result.command.military is military

    @pytest.mark.parametrize(
        "builder",
        [
            frames.read_memory_config,
            frames.read_memory_pool_size,
            frames.read_run_sequence,
            frames.read_run_time_table,
        ],
        ids=["memory config", "pool size", "run sequence", "run time table"],
    )
    def test_every_read_the_service_can_send(self, builder):
        result = payload(builder())
        assert isinstance(result.command, decode.ReadCommand)
        assert "sends nothing back" in result.command.summary


class TestSpansCoverTheFrame:
    @pytest.mark.parametrize(
        "built",
        [
            frames.write_text_file(b"A", render("<red>HI<flash_on>!")),
            # A two byte display mode shifts every later offset by one, so it
            # is the case a span table is most likely to get wrong.
            frames.write_text_file(b"A", b"HI", mode=c.MODE_STARBURST),
            frames.write_text_file(c.FILE_PRIORITY, b""),
            frames.set_memory_config([frames.FileAllocation(b"A", 256)]),
            frames.set_run_sequence([b"A", b"B"]),
            frames.set_run_sequence([]),
            frames.set_time(14, 30),
            frames.clear_memory(),
            frames.read_run_time_table(),
        ],
        ids=[
            "write",
            "two byte mode",
            "release priority",
            "memory",
            "sequence",
            "empty sequence",
            "time",
            "clear",
            "read",
        ],
    )
    def test_every_byte_of_the_frame_belongs_to_exactly_one_span(self, built):
        result = payload(built)
        assert b"".join(one.data for one in result.spans) == result.transmission.raw


class TestComplaints:
    def test_a_write_with_no_start_of_mode(self):
        result = payload(c.COMMAND_WRITE_TEXT + b"A" + b"HI")
        assert result.command.body == b"HI"
        assert not result.command.has_start_of_mode
        assert any("no start of mode" in one for one in result.complaints)

    def test_a_priority_message_the_sign_would_truncate(self):
        oversize = b"X" * (c.PRIORITY_FILE_CAPACITY + 10)
        result = payload(c.COMMAND_WRITE_TEXT + c.FILE_PRIORITY + c.SOM + b" b" + oversize)
        assert any("would truncate it" in one for one in result.complaints)

    def test_a_memory_configuration_with_a_partial_entry(self):
        built = frames.set_memory_config([frames.FileAllocation(b"A", 256)]) + b"BAU00"
        result = payload(built)
        assert any("left over" in one for one in result.complaints)

    def test_a_memory_configuration_size_that_is_not_hexadecimal(self):
        result = payload(c.COMMAND_WRITE_SPECIAL + c.SF_SET_MEMORY_CONFIG + b"AAUZZZZFFFF")
        assert any("not hexadecimal" in one for one in result.complaints)

    def test_the_priority_file_inside_a_memory_configuration(self):
        result = payload(c.COMMAND_WRITE_SPECIAL + c.SF_SET_MEMORY_CONFIG + b"0AU0100FFFF")
        assert any("outside the memory pool" in one for one in result.complaints)

    def test_a_display_mode_the_protocol_does_not_list(self):
        result = payload(c.COMMAND_WRITE_TEXT + b"A" + c.SOM + b" " + b"@" + b"HI")
        assert any("display mode" in one for one in result.complaints)

    def test_a_run_sequence_order_flag_that_is_not_t_s_or_d(self):
        result = payload(c.COMMAND_WRITE_SPECIAL + c.SF_SET_RUN_SEQUENCE + b"XUAB")
        assert any("'T', 'S' or 'D'" in one for one in result.complaints)

    def test_a_command_code_that_is_not_in_the_table(self):
        result = payload(b"QQQ")
        assert isinstance(result.command, decode.Unrecognised)
        assert any("not in the protocol's command table" in one for one in result.complaints)

    def test_a_transmission_carrying_no_command_at_all(self):
        result = payload(b"")
        assert isinstance(result.command, decode.Unrecognised)
        assert any("no command at all" in one for one in result.complaints)


class TestNothingRaises:
    @pytest.mark.parametrize(
        "built",
        [b"A", b"E", b"F", b"E$", b"E.", b"E ", b"E&", b"E'", b"A0", b"AA\x1b", b"AA\x1b "],
        ids=lambda built: repr(built),
    )
    def test_a_truncated_payload_decodes_rather_than_explodes(self, built):
        result = payload(built)
        assert result.command.summary
