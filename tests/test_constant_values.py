"""Pin every protocol byte value this service uses against the protocol document.

Why this file exists
====================

The project's stated safety story is that golden byte tests make protocol
refactoring safe. That was only half true. The golden packets in
``test_frames.py`` do pin the framing, the clock commands, the memory
configuration and the run sequence, because they assert whole transmissions
literally. But the token table, seventy-odd colours, modes, inserts and
attributes, was pinned by nothing at all.

The test that looked like it covered them does not::

    def test_every_token_in_the_table_renders():
        for token in MARKUP_TOKENS:
            assert render(token.text) == token.value

That compares the table to itself. It proves the tokenizer resolves a token's
text to its table entry, which is worth proving, but it would pass unchanged if
every byte in the table were wrong. A wrong nibble in there is invisible to CI
and surfaces as one wrong colour on a wall, months later, with no failing test
to point at.

Every assertion below carries the place in the protocol document it came from,
so this file doubles as the audit trail showing where the table's values
actually originate.

Source
======

Alpha Sign Communications Protocol, Adaptive Micro Systems, form 9708-8061E,
dated August 1 2003. Page numbers are that document's own.

``readerboard/protocol/constants.py`` cites revision F, dated March 10 2006,
which is the revision Adaptive publishes today. The two agree on every value
used here. Their pagination does not always agree: the control code table is on
page 80 of revision E and page 81 of revision F, while Table 15 is on page 21 of
both. So where a page number here and one there disagree about the same table,
that is the revision and not a mistake in either.

What this file does NOT establish
=================================

The identity of the extended characters. The document renders them as glyphs in
a Character column, and glyphs are exactly what does not survive text extraction
from a PDF. So this file pins the *structure* of the extended character set,
which the document states in words and which is a real and checkable
constraint, but it cannot confirm from the document that byte 0xA9 is a degree
sign rather than some other mark.

For those identities the evidence is empirical instead: this sign has been
displaying ``<degree>`` after an outdoor temperature for years. That is weaker
than a citation and is deliberately not dressed up as one. See
``test_extended_character_identities_are_not_verified_here``.
"""

import pytest

from readerboard.protocol import constants as c
from readerboard.protocol.markup import EXTENDED_CHARACTERS


def hexes(value: bytes) -> str:
    """Render bytes the way the protocol document writes them, for failure output."""
    return " ".join("%02XH" % byte for byte in value)


# ===========================================================================
# Packet framing
#
# Section 6.1, "Transmission packet format", and the worked examples in
# section 7.6. The five nulls are described as what "cause a sign to lock onto
# a baud rate", which the document calls autobauding.
# ===========================================================================

FRAMING = [
    (c.NUL, b"\x00", "00H, NUL, control code table page 80"),
    (c.SOH, b"\x01", "01H, Start Of Header, control code table page 80"),
    (c.STX, b"\x02", "02H, Start Of TeXt, control code table page 80"),
    (c.ETX, b"\x03", "03H, End of TeXt, control code table page 80"),
    (c.EOT, b"\x04", "04H, End Of Transmission, control code table page 80"),
    (c.SIGN_TYPE_BETABRITE, b"^", '"^" 5EH Betabrite sign, sign type code table page 15'),
    (c.SIGN_ADDRESS_BROADCAST, b"00", '"00" means all signs should listen, page 67'),
]


@pytest.mark.parametrize("actual,expected,citation", FRAMING)
def test_framing_bytes(actual, expected, citation):
    assert actual == expected, "%s: expected %s" % (citation, hexes(expected))


def test_the_wakeup_is_at_least_five_nulls():
    """The document says five; this sends six, which is harmless and proven.

    "These five <NUL>s cause a sign to lock onto a baud rate." More nulls than
    required cannot hurt, and six have driven this sign for years, so the
    constant is left alone rather than trimmed to match the letter of the text.
    """
    assert set(c.WAKEUP) == {0}
    assert len(c.WAKEUP) >= 5


# ===========================================================================
# Command codes
#
# Table 11 and the command code tables in section 6.
# ===========================================================================

COMMAND_CODES = [
    (c.COMMAND_WRITE_TEXT, b"A", '"A" (41H) is the Write TEXT file Command Code, page 67'),
    (
        c.COMMAND_WRITE_SPECIAL,
        b"E",
        '"E" (45H) = Write SPECIAL FUNCTION command, Table 15 page 21',
    ),
    (c.COMMAND_READ_SPECIAL, b"F", '"F" (46H) = Read SPECIAL FUNCTION command, section 6.2'),
]


@pytest.mark.parametrize("actual,expected,citation", COMMAND_CODES)
def test_command_codes(actual, expected, citation):
    assert actual == expected, citation


# ===========================================================================
# Special function labels
#
# Table 15, "Write SPECIAL FUNCTION Command Code format", page 21 onwards. Each
# label follows the "E" command code in the payload.
# ===========================================================================

SPECIAL_FUNCTION_LABELS = [
    (
        c.SF_SET_MEMORY_CONFIG,
        b"$",
        '"$" 24H Clear Memory/Set Memory Configuration, Table 15',
    ),
    (c.SF_SET_RUN_SEQUENCE, b".", '"." 2EH Set Run Sequence, Table 15'),
    (c.SF_MEMORY_POOL_SIZE, b"#", '"#" 23H Read Memory Pool Size, Table 16'),
    (c.SF_RUN_TIME_TABLE, b")", '")" 29H Run Time Table, Table 15'),
    (c.CMD_SET_TIME, b"\x20", '" " 20H Set Time of Day, Table 15'),
    (c.CMD_SET_DAY_OF_WEEK, b"\x26", '"&" 26H Set Day of Week, Table 15'),
    (c.CMD_SET_TIME_FORMAT, b"\x27", "\"'\" 27H Set Time Format, Table 15"),
]


@pytest.mark.parametrize("actual,expected,citation", SPECIAL_FUNCTION_LABELS)
def test_special_function_labels(actual, expected, citation):
    assert actual == expected, "%s: expected %s" % (citation, hexes(expected))


def test_the_day_of_week_range_starts_at_sunday():
    """Table 15: "1" 31H = Sunday through "7" 37H = Saturday.

    Pinned because a wrong range is accepted silently by anything that only
    checks the byte width, and "0 to 6" is the guess most people make.
    """
    from readerboard.protocol import frames

    assert frames.set_day_of_week(1) == c.COMMAND_WRITE_SPECIAL + c.CMD_SET_DAY_OF_WEEK + b"1"
    assert frames.set_day_of_week(7) == c.COMMAND_WRITE_SPECIAL + c.CMD_SET_DAY_OF_WEEK + b"7"
    with pytest.raises(frames.ProtocolError):
        frames.set_day_of_week(0)


# ===========================================================================
# Memory configuration fields
#
# Table 15, the "$" label. The record is FTPSIZEQQQQ, eleven characters per
# file.
# ===========================================================================

MEMORY_CONFIG = [
    (c.FILE_TYPE_TEXT, b"A", '"A" 41H = TEXT file, the T field'),
    (c.FILE_TYPE_STRING, b"B", '"B" 42H = STRING file, the T field'),
    (c.FILE_UNLOCKED, b"U", '"U" 55H = Unlocked, the P field'),
    (c.FILE_LOCKED, b"L", '"L" 4CH = Locked, the P field'),
    (c.FILE_PRIORITY, b"0", 'File Label "0" (30H) is the Priority TEXT file, Appendix A'),
]


@pytest.mark.parametrize("actual,expected,citation", MEMORY_CONFIG)
def test_memory_configuration_fields(actual, expected, citation):
    assert actual == expected, citation


def test_the_always_schedule_is_four_f_characters():
    """Appendix B: "Stop Time is ignored when Start Time is set to Always (FF)".

    So FF as the start makes a file permanently eligible and the stop half is
    not consulted. This is what lets a slot leave the rotation by being dropped
    from the run sequence rather than by a memory reconfiguration, which would
    erase the sign.
    """
    assert c.TEXT_SCHEDULE_ALWAYS == b"FFFF"
    assert len(c.TEXT_SCHEDULE_ALWAYS) == 4


def test_each_configured_file_costs_eleven_bytes_of_overhead():
    """Each file in a memory configuration costs eleven bytes beyond its size.

    Table 15 note 1: "The sum of all the file sizes ... plus 11 bytes of
    overhead for each file should not exceed the total amount of available
    memory in the pool."
    """
    assert c.FILE_OVERHEAD_BYTES == 11


def test_the_priority_file_holds_125_bytes():
    """The priority file is a fixed size the sign will not let anyone change.

    Section 6.1.3: "A Priority TEXT file is a special 125-byte message that does
    not need to be configured because it always exists on a sign."
    """
    assert c.PRIORITY_FILE_CAPACITY == 125


# ===========================================================================
# Run sequence
#
# Table 15, the "." label. Format KPF: order type, keyboard protection, labels.
# ===========================================================================

RUN_SEQUENCE = [
    (
        c.RUN_SEQ_BY_TIME,
        b"T",
        '"T" 54H = run according to their associated times (default)',
    ),
    (
        c.RUN_SEQ_IGNORE_TIME,
        b"S",
        '"S" 53H = run in order regardless of each file\'s run time',
    ),
    (
        c.RUN_SEQ_DELETE_AT_STOP,
        b"D",
        '"D" 44H = run according to times, then delete at the off time',
    ),
]


@pytest.mark.parametrize("actual,expected,citation", RUN_SEQUENCE)
def test_run_sequence_order_types(actual, expected, citation):
    assert actual == expected, citation


def test_the_service_does_not_use_the_deleting_order_type():
    """"D" deletes a file when it reaches its off time.

    Every file this service allocates is scheduled Always, so "D" would never
    fire, but choosing it would make a later scheduling change quietly
    destructive. The default is the non-destructive one on purpose.
    """
    from readerboard.protocol import frames

    assert frames.set_run_sequence([b"A"]) != frames.set_run_sequence(
        [b"A"], mode=c.RUN_SEQ_DELETE_AT_STOP
    )
    assert frames.set_run_sequence([b"A"]).startswith(
        c.COMMAND_WRITE_SPECIAL + c.SF_SET_RUN_SEQUENCE + c.RUN_SEQ_IGNORE_TIME
    )


# ===========================================================================
# Display position
#
# Table 12, "Write TEXT file transmission packet format", the Position field.
# ===========================================================================

POSITIONS = [
    (c.TEXT_POS_MIDDLE, b"\x20", '" " 20H Middle Line, text centered vertically'),
    (c.TEXT_POS_TOP, b"\x22", '""" 22H Top Line'),
    (c.TEXT_POS_BOTTOM, b"\x26", '"&" 26H Bottom Line'),
    (c.TEXT_POS_FILL, b"\x30", '"0" 30H Fill, all available lines centered vertically'),
]


@pytest.mark.parametrize("actual,expected,citation", POSITIONS)
def test_display_positions(actual, expected, citation):
    assert actual == expected, "%s: expected %s" % (citation, hexes(expected))


# ===========================================================================
# Standard modes
#
# Table 64, "Standard Modes", page 88.
# ===========================================================================

STANDARD_MODES = [
    (c.MODE_ROTATE, b"a", "ROTATE 61H"),
    (c.MODE_HOLD, b"b", "HOLD 62H"),
    (c.MODE_FLASH, b"c", "FLASH 63H"),
    (c.MODE_ROLLUP, b"e", "ROLL UP 65H"),
    (c.MODE_ROLLDOWN, b"f", "ROLL DOWN 66H"),
    (c.MODE_ROLLLEFT, b"g", "ROLL LEFT 67H"),
    (c.MODE_ROLLRIGHT, b"h", "ROLL RIGHT 68H"),
    (c.MODE_WIPEUP, b"i", "WIPE UP 69H"),
    (c.MODE_WIPEDOWN, b"j", "WIPE DOWN 6AH"),
    (c.MODE_WIPELEFT, b"k", "WIPE LEFT 6BH"),
    (c.MODE_WIPERIGHT, b"l", "WIPE RIGHT 6CH"),
    (c.MODE_SCROLL, b"m", "SCROLL 6DH"),
    (c.MODE_AUTO, b"o", "AUTOMODE 6FH"),
    (c.MODE_ROLLIN, b"p", "ROLL IN 70H"),
    (c.MODE_ROLLOUT, b"q", "ROLL OUT 71H"),
    (c.MODE_WIPEIN, b"r", "WIPE IN 72H"),
    (c.MODE_WIPEOUT, b"s", "WIPE OUT 73H"),
    (c.MODE_CMPRSROT, b"t", "COMPRESSED ROTATE 74H"),
]


@pytest.mark.parametrize("actual,expected,citation", STANDARD_MODES)
def test_standard_modes(actual, expected, citation):
    assert actual == expected, "Table 64 page 88, %s" % citation


# ===========================================================================
# Special modes and special graphics
#
# Tables 65 and 66, pages 88 and 89. Each is the SPECIAL mode code "n" (6EH)
# followed by a specifier character.
# ===========================================================================

SPECIAL_MODES = [
    (c.MODE_TWINKLE, b"n0", "TWINKLE 30H, Table 65"),
    (c.MODE_SPARKLE, b"n1", "SPARKLE 31H, Table 65"),
    (c.MODE_SNOW, b"n2", "SNOW 32H, Table 65"),
    (c.MODE_INTERLOCK, b"n3", "INTERLOCK 33H, Table 65"),
    (c.MODE_SWITCH, b"n4", "SWITCH 34H, Table 65"),
    (c.MODE_SLIDE, b"n5", "SLIDE 35H, Table 65"),
    (c.MODE_SPRAY, b"n6", "SPRAY 36H, Table 65"),
    (c.MODE_STARBURST, b"n7", "STARBURST 37H, Table 65"),
    (c.MODE_WELCOME, b"n8", "WELCOME 38H, Table 65"),
    (c.MODE_SLOTMACHINE, b"n9", "SLOT MACHINE 39H, Table 65"),
    (c.MODE_THANKYOU, b"nS", "THANK YOU 53H, Table 66"),
    (c.MODE_NOSMOKING, b"nU", "NO SMOKING 55H, Table 66"),
    (c.MODE_DRINKDRIVE, b"nV", "DON'T DRINK & DRIVE 56H, Table 66"),
    (c.MODE_FISH, b"nW", "RUNNING ANIMAL or FISH ANIMATION 57H, Table 66"),
    (c.MODE_FIREWORKS, b"nX", "FIREWORKS 58H, Table 66"),
    (c.MODE_BALLOONS, b"nY", "TURBO CAR or BALOON ANIMATION 59H, Table 66"),
    (c.MODE_CHERRYBOMB, b"nZ", "CHERRY BOMB 5AH, Table 66"),
]


@pytest.mark.parametrize("actual,expected,citation", SPECIAL_MODES)
def test_special_modes(actual, expected, citation):
    assert actual == expected, citation


def test_every_special_mode_is_the_special_mode_code_plus_one_specifier():
    """Every special mode is two bytes: the mode code, then one specifier.

    Table 64: SPECIAL is "n" (6EH), "followed by a Special Specifier ASCII
    character which defines one of the Special Modes".
    """
    for value, _, citation in SPECIAL_MODES:
        assert len(value) == 2, citation
        assert value[:1] == b"n", citation


class TestTheDocumentContradictsItself:
    """Table 65 gives NEWS FLASH and TRUMPET inconsistent ASCII and hex columns.

    The table reads::

        NEWS FLASH          "A"  3AH
        TRUMPET ANIMATION   "B"  3BH
        CYCLE COLORS        "C"  43H

    3AH is ":" and 3BH is ";", not "A" and "B". The two columns cannot both be
    right. CYCLE COLORS settles it: there the letter "C" and 43H agree, so for
    the lettered entries the ASCII column is the one to trust and the hex column
    has continued the numeric run 30H to 39H by mistake.

    This service therefore uses 41H and 42H, the actual letters. Recorded here
    because a future reader checking the table against this code would otherwise
    find an apparent bug and "fix" it into a real one.
    """

    def test_newsflash_uses_the_letter_not_the_hex_column(self):
        assert c.MODE_NEWSFLASH == b"nA"
        assert c.MODE_NEWSFLASH[1:] == b"\x41"

    def test_trumpet_uses_the_letter_not_the_hex_column(self):
        assert c.MODE_TRUMPET == b"nB"
        assert c.MODE_TRUMPET[1:] == b"\x42"

    def test_cycle_colors_is_the_entry_that_settles_it(self):
        # "C" and 43H agree here, which is why the letters win above.
        assert ord("C") == 0x43


# ===========================================================================
# Control codes, 00H to 1FH
#
# Section 7.7.1.1, "Control codes (00 - 1FH)", page 80.
# ===========================================================================

CONTROL_CODES = [
    (c.NO_HOLD_SPEED, b"\x09", "09H No Hold speed"),
    (c.CR, b"\x0d", "0DH New line"),
    (c.NEW_PAGE, b"\x0c", "0CH New page, start of next display page"),
    (c.WIDE_CHARS_OFF, b"\x11", "11H Disable wide characters"),
    (c.WIDE_CHARS_ON, b"\x12", "12H Enable wide characters"),
    (c.CURTIME_INSERT, b"\x13", "13H Call Time, time of day will be called up"),
    (c.SPEED_1, b"\x15", "15H Speed 1 (slowest)"),
    (c.SPEED_2, b"\x16", "16H Speed 2"),
    (c.SPEED_3, b"\x17", "17H Speed 3"),
    (c.SPEED_4, b"\x18", "18H Speed 4"),
    (c.SPEED_5, b"\x19", "19H Speed 5 (fastest)"),
    (c.SOM, b"\x1b", "1BH Start of Mode field"),
]


@pytest.mark.parametrize("actual,expected,citation", CONTROL_CODES)
def test_control_codes(actual, expected, citation):
    assert actual == expected, "page 80, %s" % citation


TWO_BYTE_TOGGLES = [
    (c.DBL_HEIGHT_CHARS_OFF, b"\x05\x30", "05H + 0 = Double height off (default)"),
    (c.DBL_HEIGHT_CHARS_ON, b"\x05\x31", "05H + 1 = Double height on"),
    (c.TRUE_DESCENDERS_OFF, b"\x06\x30", "06H + 0 = True descenders off (default)"),
    (c.TRUE_DESCENDERS_ON, b"\x06\x31", "06H + 1 = True descenders on"),
    (c.CHAR_FLASH_OFF, b"\x07\x30", "07H + 0 = Character flash off (default)"),
    (c.CHAR_FLASH_ON, b"\x07\x31", "07H + 1 = Character flash on"),
    (c.FIXED_WIDTH_OFF, b"\x1e\x30", "1EH + 0 = Proportional characters (default)"),
    (c.FIXED_WIDTH_ON, b"\x1e\x31", "1EH + 1 = Fixed width left justified"),
]


@pytest.mark.parametrize("actual,expected,citation", TWO_BYTE_TOGGLES)
def test_two_byte_toggles(actual, expected, citation):
    """These are the pairs an earlier version of this table had inverted.

    Four names had their on and off senses swapped relative to the byte they
    were bound to: an "ON" constant holding the "0" suffix that the document
    defines as off. The values were right and the names were wrong, which is the
    worst way round, because reading the code told you the opposite of what it
    did.
    """
    assert actual == expected, "page 80, %s" % citation


DATE_INSERTS = [
    (c.CURDATE_MMDDYY_SLASH, b"\x0b\x30", "0BH + 0 = MM/DD/YY"),
    (c.CURDATE_DDMMYY_SLASH, b"\x0b\x31", "0BH + 1 = DD/MM/YY"),
    (c.CURDATE_MMMDDYYYY, b"\x0b\x38", "0BH + 8 = MMM.DD, YYYY"),
    (c.CURDATE_WEEKDAYY, b"\x0b\x39", "0BH + 9 = Day of week"),
]


@pytest.mark.parametrize("actual,expected,citation", DATE_INSERTS)
def test_date_inserts(actual, expected, citation):
    assert actual == expected, "page 80, Call date 2-byte format, %s" % citation


# ===========================================================================
# Colours
#
# Section 7.7.1.1, the 1CH "Select character color" entry, page 81.
# ===========================================================================

COLOURS = [
    (c.TEXT_COLOR_RED, b"\x1c\x31", "1CH + 1 = Red"),
    (c.TEXT_COLOR_GREEN, b"\x1c\x32", "1CH + 2 = Green"),
    (c.TEXT_COLOR_AMBER, b"\x1c\x33", "1CH + 3 = Amber"),
    (c.TEXT_COLOR_DIMRED, b"\x1c\x34", "1CH + 4 = Dim red"),
    (c.TEXT_COLOR_DIMGREEN, b"\x1c\x35", "1CH + 5 = Dim green"),
    (c.TEXT_COLOR_BROWN, b"\x1c\x36", "1CH + 6 = Brown"),
    (c.TEXT_COLOR_ORANGE, b"\x1c\x37", "1CH + 7 = Orange"),
    (c.TEXT_COLOR_YELLOW, b"\x1c\x38", "1CH + 8 = Yellow"),
    (c.TEXT_COLOR_RAINBOW1, b"\x1c\x39", "1CH + 9 = Rainbow 1"),
    (c.TEXT_COLOR_RAINBOW2, b"\x1c\x41", "1CH + A = Rainbow 2"),
    (c.TEXT_COLOR_MIX, b"\x1c\x42", "1CH + B = Color mix"),
    (c.TEXT_COLOR_AUTO, b"\x1c\x43", "1CH + C = Autocolor"),
]


@pytest.mark.parametrize("actual,expected,citation", COLOURS)
def test_colours(actual, expected, citation):
    assert actual == expected, "page 81, %s" % citation


def test_the_colour_run_is_contiguous_then_jumps_to_the_letters():
    """Red through Rainbow 1 are "1" to "9"; Rainbow 2 onward are "A" to "C".

    Worth pinning as a shape as well as as values, because the jump from 39H to
    41H is exactly where an off-by-one in a hand written table lands.
    """
    numbered = [value for value, _, _ in COLOURS[:9]]
    for index, value in enumerate(numbered):
        assert value == b"\x1c" + bytes([ord("1") + index])

    lettered = [value for value, _, _ in COLOURS[9:]]
    for index, value in enumerate(lettered):
        assert value == b"\x1c" + bytes([ord("A") + index])


# ===========================================================================
# Extended characters
#
# Section 7.7.2, "Extended character set (80 - C1H)", page 83.
# ===========================================================================


def test_the_extended_set_maps_one_byte_form_to_two_byte_form():
    """The document tabulates 80H as 08H + 20H, 81H as 08H + 21H, and so on.

    So the single byte form and the two byte form address the same character,
    offset by 60H. This is the one thing about the extended set that the
    document states in words rather than in glyphs, so it is the one thing
    checkable without reading the pictures.

    It also settles a question the code raises: XC_DEGREES is 08H 49H and
    DEGREES is A9H, and those are the same character written two ways, not two
    different marks.
    """
    assert c.XC_DEGREES == b"\x08\x49"
    assert c.DEGREES == b"\xa9"
    assert c.DEGREES[0] == 0x80 + (c.XC_DEGREES[1] - 0x20)


def _extended_constants() -> tuple[dict[int, str], dict[int, str]]:
    """Find both halves of the extended table by looking, not by being told.

    An earlier version of this file listed the pairs by hand and covered
    thirteen of them. It missed the twenty-six lowercase named accented
    characters that ``markup.py`` actually uses, which is precisely the kind of
    gap a hand written list produces and precisely the kind this file exists to
    close. So the pairs are discovered instead: add a constant to the table and
    it is covered from that moment, with nobody having to remember.

    Returns two maps from character code to constant name, one built from the
    two byte forms and one from the single byte forms.
    """
    values = {
        name: value
        for name, value in vars(c).items()
        if isinstance(value, bytes) and not name.startswith("_")
    }

    two_byte: dict[int, str] = {}
    single: dict[int, str] = {}

    for name, value in values.items():
        # 08H also introduces the two temperature displays, at offsets 1CH and
        # 1DH, which sit below the extended character range and are not
        # characters in this sense.
        if len(value) == 2 and value[0] == 0x08 and 0x20 <= value[1] <= 0x61:
            two_byte[0x80 + (value[1] - 0x20)] = name
        elif len(value) == 1 and 0x80 <= value[0] <= 0xC1:
            single[value[0]] = name

    return two_byte, single


def test_the_two_halves_of_the_extended_table_agree_exactly():
    """Every character has both forms, and they address the same code.

    Page 83 tabulates 80H as 08H + 20H, 81H as 08H + 21H, and so on to the end
    of the range. So the table is a bijection: each character has a single byte
    form and a two byte form, and neither half may contain a code the other
    lacks.

    This is what pins the values. A mistranscribed byte in either half breaks
    the correspondence and fails here, which is the check this table went
    without for a long time. It covers all sixty-six characters, including the twenty-six
    ``markup.py`` maps accented text onto.
    """
    two_byte, single = _extended_constants()

    missing_single = sorted(set(two_byte) - set(single))
    missing_two_byte = sorted(set(single) - set(two_byte))

    assert not missing_single, "no single byte form for %s" % [
        "%02XH (%s)" % (code, two_byte[code]) for code in missing_single
    ]
    assert not missing_two_byte, "no two byte form for %s" % [
        "%02XH (%s)" % (code, single[code]) for code in missing_two_byte
    ]


def test_the_extended_table_is_the_size_the_document_describes():
    """Page 83 heads the range "80 - C1H", which is sixty-six characters.

    Pinned as a count so that losing an entry shows up as a failure here rather
    than as a character that silently stops rendering.
    """
    two_byte, single = _extended_constants()

    assert len(two_byte) == 66
    assert len(single) == 66
    assert min(single) == 0x80
    assert max(single) == 0xC1


def test_every_extended_character_used_by_markup_has_both_forms():
    """The characters that actually reach the sign are covered, not just the table.

    ``markup.py`` renders accented text by mapping Unicode onto the single byte
    forms. Each one it uses must be a real entry with both halves present, or
    the sign is being sent a byte that means nothing to it.
    """
    _, single = _extended_constants()

    for character, value in EXTENDED_CHARACTERS.items():
        assert len(value) == 1, character
        assert value[0] in single, "%r maps to %s, which is not in the extended table" % (
            character,
            hexes(value),
        )


def test_extended_character_identities_are_not_verified_here():
    """State the limit of this file plainly, so nobody mistakes its coverage.

    Which glyph each extended byte draws is given in the document only as a
    picture in a Character column. That column does not survive text extraction,
    so nothing above establishes that A9H is a degree sign rather than some
    other mark.

    The evidence for the identities is that this sign has been showing a degree
    symbol after the outdoor temperature for years, driven by A9H. That is
    empirical, and good enough to rely on, but it is not a citation and is not
    presented as one. If the extended table is ever rewritten, these identities
    are the part that needs a human looking at the printed document.
    """
    assert EXTENDED_CHARACTERS["°"] == c.DEGREES
    assert "°" in EXTENDED_CHARACTERS


# ===========================================================================
# File labels
#
# Appendix A, "Valid File Labels", page 49.
# ===========================================================================


def test_the_pool_avoids_every_reserved_label():
    """The file pool never collides with a label the sign has spoken for.

    Appendix A reserves "0" for the priority file, and notes that labels "1"
    through "5" become Target files if the counter feature is used. The pool is
    A to Z, so none of them can collide, but the constant exists so that a
    future change to the pool cannot wander into them unnoticed.
    """
    assert set(c.TEXT_FILE_LABELS).isdisjoint(set(c.RESERVED_FILE_LABELS))
    assert c.FILE_PRIORITY in c.RESERVED_FILE_LABELS


def test_every_pool_label_is_a_valid_file_label():
    """Appendix A: "File Labels can be anywhere in the range 20H through 7EH"."""
    for label in c.TEXT_FILE_LABELS:
        assert len(label) == 1
        assert 0x20 <= label[0] <= 0x7E


def test_the_pool_is_a_through_z():
    assert c.TEXT_FILE_LABELS[0] == b"A"
    assert c.TEXT_FILE_LABELS[-1] == b"Z"
    assert len(c.TEXT_FILE_LABELS) == 26


# ===========================================================================
# Protocol generation
# ===========================================================================


def test_this_targets_alpha_1_0():
    """The compatibility matrix lists the Betabrite as EZ KEY II and Alpha 1.0.

    Nothing the document marks Alpha 2.0 or 3.0 may be used, which rules out the
    E$$$$ clear-memory-and-compact-flash command, programmable sounds and the
    ACK/NAK response feature. The constant records the target so the rule has
    somewhere to live other than a comment.
    """
    assert c.PROTOCOL_GENERATION == "1.0"


def test_the_documented_inter_byte_timeout():
    """The inter-byte timeout for a standard packet is one second.

    inter_packet_delay is configured well under this by default and the spike
    measures what the sign really needs, but the documented figure is the
    ceiling any measurement should be sanity checked against.
    """
    assert c.INTER_BYTE_TIMEOUT_SECONDS == 1.0


# ===========================================================================
# Character attributes
#
# Control code 1DH in Appendix G, "Select character attribute (3-byte format)".
# The document's own description of the shape: "1st byte is control code; 2nd
# byte is the attribute; and 3rd byte specifies either ON ["1" (31H)] or OFF
# ["0" (30H)]", and OFF is the default for every one of them.
#
# This table was the largest group in constants.py that nothing here pinned.
# ===========================================================================

CHARACTER_ATTRIBUTES = [
    (c.CHAR_ATTRIB_WIDE_ON, b"\x1d\x30\x31", '1DH + "0" + "1" wide on'),
    (c.CHAR_ATTRIB_WIDE_OFF, b"\x1d\x30\x30", '1DH + "0" + "0" wide off'),
    (c.CHAR_ATTRIB_DBLW_ON, b"\x1d\x31\x31", '1DH + "1" + "1" double wide on'),
    (c.CHAR_ATTRIB_DBLW_OFF, b"\x1d\x31\x30", '1DH + "1" + "0" double wide off'),
    (c.CHAR_ATTRIB_DBLH_ON, b"\x1d\x32\x31", '1DH + "2" + "1" double high on'),
    (c.CHAR_ATTRIB_DBLH_OFF, b"\x1d\x32\x30", '1DH + "2" + "0" double high off'),
    (c.CHAR_ATTRIB_DESC_ON, b"\x1d\x33\x31", '1DH + "3" + "1" true descenders on'),
    (c.CHAR_ATTRIB_DESC_OFF, b"\x1d\x33\x30", '1DH + "3" + "0" true descenders off'),
    (c.CHAR_ATTRIB_FIX_ON, b"\x1d\x34\x31", '1DH + "4" + "1" fixed width on'),
    (c.CHAR_ATTRIB_FIX_OFF, b"\x1d\x34\x30", '1DH + "4" + "0" fixed width off'),
    (c.CHAR_ATTRIB_FNCY_ON, b"\x1d\x35\x31", '1DH + "5" + "1" fancy on'),
    (c.CHAR_ATTRIB_FNCY_OFF, b"\x1d\x35\x30", '1DH + "5" + "0" fancy off'),
]


@pytest.mark.parametrize("actual,expected,citation", CHARACTER_ATTRIBUTES)
def test_character_attributes(actual, expected, citation):
    assert actual == expected, "%s: expected %s" % (citation, hexes(expected))


def test_every_character_attribute_is_an_on_off_pair():
    """Every attribute has both halves, and they differ only in the last byte.

    A missing half is the failure worth catching: it leaves an attribute that
    can be switched on with nothing able to switch it off, and the sign then
    keeps it until something else rewrites the file. Discovered from the module
    rather than from the list above, so a constant added later is covered from
    the moment it exists.
    """
    ons = [n for n in dir(c) if n.startswith("CHAR_ATTRIB_") and n.endswith("_ON")]
    assert ons, "the character attribute table has gone missing"
    for on in ons:
        off = on[: -len("_ON")] + "_OFF"
        assert hasattr(c, off), "%s has no %s" % (on, off)
        assert getattr(c, on)[:2] == getattr(c, off)[:2]
        assert getattr(c, on)[2:] == b"1"
        assert getattr(c, off)[2:] == b"0"


def test_the_attributes_this_sign_has_no_hardware_for_are_absent():
    """36H auxiliary port and 37H shadow characters name other signs.

    The document marks the first "Series 4000 & 7000 signs only" and the second
    "Betabrite model 1036 and AlphaPremiere 9000 signs only". Both sit in the
    same table as the six that are here, which is exactly why it is worth a
    test rather than a comment.
    """
    for value in (b"\x1d\x36", b"\x1d\x37"):
        for name in dir(c):
            if name.startswith("CHAR_ATTRIB_"):
                assert not getattr(c, name).startswith(value), name


# ===========================================================================
# What the Alpha 1.0 generation leaves out
#
# PROTOCOL_GENERATION records that a Betabrite speaks Alpha 1.0 and EZ KEY II
# only. These pin the other half of that: the entries the document lists in
# tables this service does use, which may not be picked up from them.
# ===========================================================================


def test_the_alpha_3_modes_are_absent():
    """EXPLODE 75H and CLOCK 76H are marked Alpha 3.0 protocol.

    Both sit in the Standard Modes table, immediately after COMPRESSED ROTATE,
    which is here. A mode this sign cannot run is worse than a missing one: it
    fails on the wall rather than in review.
    """
    for name in ("MODE_EXPLODE", "MODE_CLOCK"):
        assert not hasattr(c, name), "%s is Alpha 3.0 only" % name


def test_the_alpha_3_display_positions_are_absent():
    """Left 31H and Right 32H are marked Alpha 3.0 protocol only.

    Same table as the four positions pinned above, same reasoning.
    """
    for name in ("TEXT_POS_LEFT", "TEXT_POS_RIGHT"):
        assert not hasattr(c, name), "%s is Alpha 3.0 only" % name
