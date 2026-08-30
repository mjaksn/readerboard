"""The scanner has to survive a stream that arrives however TCP feels like."""

from readerboard.protocol import frames
from signsim.framing import FrameScanner

HELLO = frames.packet(frames.write_text_file(b"A", b"HI"))


class TestWholeFrames:
    def test_one_frame_in_one_read(self):
        found = FrameScanner().feed(HELLO)
        assert len(found) == 1
        assert found[0].payload == b"AA\x1b bHI"
        assert found[0].sign_type == b"^"
        assert found[0].address == b"00"
        assert found[0].complaints == ()

    def test_two_frames_in_one_read(self):
        found = FrameScanner().feed(HELLO + HELLO)
        assert len(found) == 2
        assert all(one.payload == b"AA\x1b bHI" for one in found)

    def test_the_raw_frame_excludes_the_wakeup_nulls(self):
        found = FrameScanner().feed(HELLO)
        assert found[0].raw.startswith(b"\x01")
        assert found[0].raw.endswith(b"\x04")
        assert found[0].wakeup_nulls == 6


class TestSplitReads:
    def test_a_frame_split_one_byte_at_a_time(self):
        scanner = FrameScanner()
        found = []
        for index in range(len(HELLO)):
            found.extend(scanner.feed(HELLO[index : index + 1]))
        assert len(found) == 1
        assert found[0].payload == b"AA\x1b bHI"

    def test_nothing_is_emitted_until_the_eot_arrives(self):
        scanner = FrameScanner()
        assert scanner.feed(HELLO[:-1]) == []
        assert scanner.pending_bytes > 0
        assert len(scanner.feed(HELLO[-1:])) == 1
        assert scanner.pending_bytes == 0

    def test_a_frame_split_across_the_header(self):
        scanner = FrameScanner()
        assert scanner.feed(HELLO[:8]) == []
        assert len(scanner.feed(HELLO[8:])) == 1


class TestComplaints:
    def test_too_few_wakeup_nulls(self):
        found = FrameScanner().feed(HELLO[4:])
        assert len(found) == 1
        assert any("wakeup nulls" in one for one in found[0].complaints)

    def test_a_full_wakeup_draws_no_complaint(self):
        assert FrameScanner().feed(HELLO)[0].complaints == ()

    def test_junk_before_a_header_is_kept_and_reported(self):
        found = FrameScanner().feed(b"noise" + HELLO)
        assert found[0].junk_before == b"noise"
        assert found[0].junk_before_count == 5
        assert any("neither a wakeup null nor a header" in one for one in found[0].complaints)

    def test_a_sign_type_that_is_not_the_betabrite(self):
        found = FrameScanner().feed(frames.packet(b"AA\x1b bHI", sign_type=b"Z"))
        assert any("not the BetaBrite" in one for one in found[0].complaints)

    def test_junk_is_attributed_to_the_next_frame_only(self):
        scanner = FrameScanner()
        first = scanner.feed(b"junk" + HELLO)[0]
        second = scanner.feed(HELLO)[0]
        assert first.junk_before_count == 4
        assert second.junk_before_count == 0


class TestResynchronising:
    def test_a_frame_with_no_eot_is_closed_by_the_next_header(self):
        found = FrameScanner().feed(HELLO[:-1] + HELLO)
        assert len(found) == 2
        assert found[0].is_truncated
        assert any("no EOT" in one for one in found[0].complaints)
        assert not found[1].is_truncated
        assert found[1].payload == b"AA\x1b bHI"

    def test_a_stray_soh_that_is_not_a_header_is_skipped(self):
        # 0x01 with no STX four bytes later is not the start of anything, so it
        # is treated as noise rather than as the loss of the rest of the stream.
        found = FrameScanner().feed(b"\x01zzzz" + HELLO)
        assert len(found) == 1
        assert found[0].payload == b"AA\x1b bHI"
        assert found[0].junk_before_count == 5

    def test_noise_alone_never_produces_a_frame(self):
        scanner = FrameScanner()
        assert scanner.feed(b"nothing here at all") == []
        assert scanner.pending_bytes == 0


class TestResyncValidatesTheCandidateHeader:
    def test_a_payload_byte_of_0x01_does_not_truncate_the_frame(self):
        # A DOTS picture is binary and may hold 0x01 anywhere. Treating each one
        # as a new header cut the frame in half and lost the remainder.
        built = frames.packet(b"I" + b"A" + bytes([0x01, 0x02, 0x03, 0x05]))
        found = FrameScanner().feed(built)
        assert len(found) == 1
        assert not found[0].is_truncated
        assert found[0].payload == b"IA\x01\x02\x03\x05"
        assert found[0].complaints == ()

    def test_an_eot_inside_a_payload_still_ends_the_frame(self):
        # Not a bug, and not fixable: the protocol gives no way to escape a
        # 0x04, so a binary payload carrying one is ambiguous on the wire.
        found = FrameScanner().feed(frames.packet(b"IA" + bytes([0x04, 0x05])))
        assert found[0].payload == b"IA"

    def test_a_real_second_header_still_closes_an_unterminated_frame(self):
        found = FrameScanner().feed(HELLO[:-1] + HELLO)
        assert len(found) == 2
        assert found[0].is_truncated
        assert not found[1].is_truncated

    def test_a_candidate_header_at_the_very_end_waits_for_more_bytes(self):
        scanner = FrameScanner()
        # The trailing 0x01 could be a payload byte or the start of a header,
        # and nothing yet says which. Guessing either way is wrong.
        assert scanner.feed(b"\x00" * 6 + b"\x01^00\x02I" + b"\x01") == []
        found = scanner.feed(b"\x02\x03\x04")
        assert len(found) == 1
        assert found[0].payload == b"I\x01\x02\x03"


class TestTheBufferIsBounded:
    def test_a_frame_that_never_ends_is_given_up_on(self):
        from signsim.framing import MAX_FRAME_BYTES

        scanner = FrameScanner()
        found = scanner.feed(b"\x00" * 6 + b"\x01^00\x02")
        assert found == []
        found = scanner.feed(b"Z" * (MAX_FRAME_BYTES + 1))
        assert len(found) == 1
        assert found[0].is_truncated
        assert any("past anything the sign has room for" in one for one in found[0].complaints)
        assert scanner.pending_bytes == 0

    def test_the_scanner_recovers_after_giving_up(self):
        from signsim.framing import MAX_FRAME_BYTES

        scanner = FrameScanner()
        scanner.feed(b"\x01^00\x02" + b"Z" * (MAX_FRAME_BYTES + 1))
        found = scanner.feed(HELLO)
        assert len(found) == 1
        assert found[0].payload == b"AA\x1b bHI"


class TestReset:
    def test_reset_drops_a_part_read_frame(self):
        scanner = FrameScanner()
        assert scanner.feed(HELLO[:12]) == []
        scanner.reset()
        assert scanner.pending_bytes == 0
        found = scanner.feed(HELLO)
        assert len(found) == 1
        assert not found[0].is_truncated
        # The wakeup nulls counted for the abandoned frame must not be carried
        # over onto the next one, or it draws a complaint it does not deserve.
        assert found[0].complaints == ()
