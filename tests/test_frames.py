"""Golden byte tests for the frame builders.

These assert whole packets against the protocol document rather than against
whatever the code currently produces, which is what makes it safe to refactor
the protocol package later. When one of these fails, the frame builder is wrong,
not the test.
"""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol import frames


class TestPacket:
    def test_framing_bytes_surround_the_payload(self):
        assert frames.packet(b"PAYLOAD") == (
            b"\x00\x00\x00\x00\x00\x00" b"\x01" b"^" b"00" b"\x02" b"PAYLOAD" b"\x04"
        )

    def test_address_and_type_can_be_overridden(self):
        built = frames.packet(b"X", sign_type=c.SIGN_TYPE_ALL, address=b"0A")
        assert built == b"\x00\x00\x00\x00\x00\x00\x01Z0A\x02X\x04"


class TestWriteTextFile:
    def test_a_hold_message_to_file_a(self):
        assert frames.write_text_file(b"A", b"HI") == b"A" b"A" b"\x1b" b" " b"b" b"HI"

    def test_mode_and_position_appear_in_order(self):
        built = frames.write_text_file(
            b"B", b"HI", mode=c.MODE_ROTATE, position=c.TEXT_POS_FILL
        )
        assert built == b"AB\x1b0aHI"

    def test_a_two_byte_mode_is_passed_through_whole(self):
        built = frames.write_text_file(b"C", b"HI", mode=c.MODE_STARBURST)
        assert built == b"AC\x1b n7HI"

    def test_an_empty_body_blanks_the_file(self):
        assert frames.write_text_file(b"A", b"") == b"AA\x1b b"

    def test_a_multi_byte_label_is_rejected(self):
        with pytest.raises(frames.ProtocolError, match="exactly one byte"):
            frames.write_text_file(b"AB", b"HI")


class TestPriorityFile:
    def test_clearing_writes_an_empty_body_to_file_zero(self):
        assert frames.clear_priority_file() == b"A0\x1b b"

    def test_a_message_within_the_capacity_is_allowed(self):
        body = b"X" * c.PRIORITY_FILE_CAPACITY
        assert frames.write_text_file(c.FILE_PRIORITY, body).endswith(body)

    def test_a_message_over_the_capacity_is_rejected(self):
        # The sign fixes the priority file at 125 bytes and will not let that
        # change, so this has to fail here rather than silently truncate.
        with pytest.raises(frames.ProtocolError, match="125 bytes"):
            frames.write_text_file(c.FILE_PRIORITY, b"X" * (c.PRIORITY_FILE_CAPACITY + 1))


class TestMemoryConfiguration:
    def test_one_text_file(self):
        built = frames.set_memory_config([frames.FileAllocation(b"A", 256)])
        assert built == b"E$" b"A" b"A" b"U" b"0100" b"FFFF"

    def test_several_files_concatenate_in_order(self):
        built = frames.set_memory_config(
            [
                frames.FileAllocation(b"A", 256),
                frames.FileAllocation(b"B", 256, locked=True),
            ]
        )
        assert built == b"E$" + b"AAU0100FFFF" + b"BAL0100FFFF"

    def test_the_size_is_four_uppercase_hex_digits(self):
        built = frames.set_memory_config([frames.FileAllocation(b"A", 4095)])
        assert built.endswith(b"0FFFFFFF")

    def test_the_priority_file_cannot_be_allocated(self):
        with pytest.raises(frames.ProtocolError, match="priority file"):
            frames.FileAllocation(c.FILE_PRIORITY, 125)

    def test_a_duplicate_label_is_rejected(self):
        with pytest.raises(frames.ProtocolError, match="same file twice"):
            frames.set_memory_config(
                [frames.FileAllocation(b"A", 256), frames.FileAllocation(b"A", 256)]
            )

    def test_an_empty_configuration_is_rejected(self):
        with pytest.raises(frames.ProtocolError, match="at least one file"):
            frames.set_memory_config([])

    def test_an_impossible_capacity_is_rejected(self):
        with pytest.raises(frames.ProtocolError, match="between 1 and 65535"):
            frames.FileAllocation(b"A", 0)


class TestRunSequence:
    def test_labels_appear_in_the_order_given(self):
        assert frames.set_run_sequence([b"A", b"B", b"C"]) == b"E.SUABC"

    def test_the_time_honouring_mode(self):
        built = frames.set_run_sequence([b"A"], mode=c.RUN_SEQ_BY_TIME)
        assert built == b"E.TUA"

    def test_locking_the_sequence(self):
        assert frames.set_run_sequence([b"A"], locked=True) == b"E.SLA"

    def test_an_empty_sequence_plays_nothing(self):
        # Meaningful rather than an error: this is an emptied registry.
        assert frames.set_run_sequence([]) == b"E.SU"

    def test_a_duplicate_label_is_rejected(self):
        with pytest.raises(frames.ProtocolError, match="same file twice"):
            frames.set_run_sequence([b"A", b"A"])


class TestClockCommands:
    def test_set_time_pads_to_four_digits(self):
        assert frames.set_time(9, 5) == b"E\x20" b"0905"

    def test_set_time_at_midnight(self):
        assert frames.set_time(0, 0) == b"E\x200000"

    def test_set_time_late(self):
        assert frames.set_time(23, 59) == b"E\x202359"

    @pytest.mark.parametrize("hour,minute", [(24, 0), (-1, 0), (0, 60), (0, -1)])
    def test_an_impossible_time_is_rejected(self, hour, minute):
        with pytest.raises(frames.ProtocolError):
            frames.set_time(hour, minute)

    def test_set_day_of_week(self):
        assert frames.set_day_of_week(1) == b"E\x26" b"1"
        assert frames.set_day_of_week(7) == b"E\x267"

    @pytest.mark.parametrize("day", [0, 8])
    def test_an_impossible_day_is_rejected(self, day):
        with pytest.raises(frames.ProtocolError, match="between 1 and 7"):
            frames.set_day_of_week(day)

    def test_set_time_format(self):
        assert frames.set_time_format(military=True) == b"E\x27" b"M"
        assert frames.set_time_format(military=False) == b"E\x27S"


def test_the_full_transmission_for_a_temperature_message():
    """One end to end golden packet, of the kind Home Assistant produces."""
    from readerboard.protocol.markup import render

    body = render("<green>18.4<degree> <red><time>")
    built = frames.packet(frames.write_text_file(b"A", body, mode=c.MODE_HOLD))
    assert built == (
        b"\x00\x00\x00\x00\x00\x00"
        b"\x01^00\x02"
        b"AA\x1b b"
        b"\x1c2" b"18.4" b"\x08I" b" " b"\x1c1" b"\x13"
        b"\x04"
    )


class TestClearMemory:
    def test_it_is_the_bare_special_function(self):
        # The protocol spells clearing memory as "E$" with nothing after it.
        assert frames.clear_memory() == b"E$"

    def test_an_empty_allocation_list_does_not_silently_clear_the_sign(self):
        with pytest.raises(frames.ProtocolError, match="clear_memory"):
            frames.set_memory_config([])


class TestMemoryClaimed:
    def test_each_file_costs_its_size_plus_eleven_bytes_of_overhead(self):
        claimed = frames.memory_claimed([frames.FileAllocation(b"A", 256)])
        assert claimed == 256 + c.FILE_OVERHEAD_BYTES

    def test_it_sums_across_the_pool(self):
        pool = [frames.FileAllocation(bytes([label]), 256) for label in b"ABCD"]
        assert frames.memory_claimed(pool) == 4 * (256 + c.FILE_OVERHEAD_BYTES)
