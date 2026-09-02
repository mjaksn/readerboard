"""Byte constants for the Alpha Sign Communications Protocol.

Every value in this module is transcribed from the protocol document itself:
the Alpha Sign Communications Protocol, Adaptive Micro Systems, form 9708-8061,
revision F, dated March 10 2006. Each section below cites the table or page it
came from, so any value can be checked against the source without leaving the
file.

``tests/test_constant_values.py`` pins these values against the same document
with a citation per assertion. That file, not this one, is what proves a value
is right; this module is only where the values live. If you add a constant
here, pin it there.

Two conventions of the document are worth knowing when reading the citations.
It writes a byte as two hex digits followed by ``H``, so ``41H`` is 0x41, and
it gives multi-byte sequences as a control code plus an offset, written
``08H + 49H``.

Three places where this module deliberately does not follow the document, each
noted again at the point it matters:

- ``WAKEUP`` sends six nulls where the document specifies five. More nulls than
  required is harmless, and six has driven this sign for years.
- ``FILE_TYPE_DOTS`` is ``D`` (44H). The document's own memory configuration
  section prints ``"D" 43H``, which is internally inconsistent, since 43H is
  ``C``. The character is taken as authoritative.
- ``MODE_NEWSFLASH`` and ``MODE_TRUMPET`` use 41H and 42H where Table 66 prints
  3AH and 3BH against the characters ``A`` and ``B``. See the note above those
  two constants.
"""


# ==========================================================================
# Packet framing
# ==========================================================================
# The frame every transmission uses, from Appendix G's control code table on
# document page 81 and the packet layouts in section 6.
#
#     WAKEUP  SOH  type  address  STX  <command and payload>  EOT
#
# The document describes the wakeup nulls as what "cause a sign to lock onto a
# baud rate" and specifies five of them. Six are sent here. More than required is
# harmless and six is what has driven this sign for years, so it is left alone
# rather than trimmed to match the letter of the document.

WAKEUP = b"\x00\x00\x00\x00\x00\x00"
NUL = b"\x00"
SOH = b"\x01"
STX = b"\x02"
ETX = b"\x03"
EOT = b"\x04"
SOM = b"\x1b"
SIGN_ADDRESS_BROADCAST = b"00"

# ==========================================================================
# Sign type codes
# ==========================================================================
# The type byte that follows SOH, naming which family of sign the packet is
# addressed to. A BetaBrite answers to "^" (5EH). The rest are carried because
# the simulator names any type byte it decodes, not because this service sends
# them.

SIGN_TYPE_SERIAL_CLOCK = b"\""
SIGN_TYPE_RESPONSE = b"0"
SIGN_TYPE_ONE_LINE = b"1"
SIGN_TYPE_TWO_LINE = b"2"
SIGN_TYPE_ALL_VERIFY = b"?"
SIGN_TYPE_430I = b"C"
SIGN_TYPE_440I = b"D"
SIGN_TYPE_460I = b"E"
SIGN_TYPE_790I = b"U"
SIGN_TYPE_ALL = b"Z"
SIGN_TYPE_BETABRITE = b"^"
SIGN_TYPE_4120C = b"a"
SIGN_TYPE_4160C = b"b"
SIGN_TYPE_4200C = b"c"
SIGN_TYPE_4240C = b"d"
SIGN_TYPE_215 = b"e"
SIGN_TYPE_215C = b"f"
SIGN_TYPE_4120R = b"g"
SIGN_TYPE_4160R = b"h"
SIGN_TYPE_4200R = b"i"
SIGN_TYPE_4240R = b"j"
SIGN_TYPE_300 = b"k"
SIGN_TYPE_7000 = b"l"
SIGN_TYPE_SOLAR_96X16 = b"m"
SIGN_TYPE_SOLAR_128X16 = b"n"
SIGN_TYPE_SOLAR_160X16 = b"o"
SIGN_TYPE_SOLAR_192X16 = b"p"
SIGN_TYPE_SOLAR_PPD = b"q"
SIGN_TYPE_DIRECTOR = b"r"
SIGN_TYPE_4080C = b"t"
SIGN_TYPE_2X0C = b"u"
SIGN_TYPE_ALL_CONFIG = b"z"

# ==========================================================================
# Command codes
# ==========================================================================
# The command code that opens the data field, from Table 12 (Write TEXT,
# document page 18), Table 15 (Write SPECIAL FUNCTION, page 21) and Table 16
# (Read SPECIAL FUNCTION, page 29).

COMMAND_WRITE_TEXT = b"A"
COMMAND_READ_TEXT = b"B"
COMMAND_WRITE_SPECIAL = b"E"
COMMAND_READ_SPECIAL = b"F"
COMMAND_WRITE_STRING = b"G"
COMMAND_READ_STRING = b"H"
COMMAND_WRITE_DOTS = b"I"
COMMAND_READ_DOTS = b"J"
COMMAND_WRITE_ALPHA_DOTS = b"M"
COMMAND_READ_ALPHA_DOTS = b"N"
COMMAND_ALPHA_BULLETIN = b"O"

# ==========================================================================
# Special function labels for the sign's clock
# ==========================================================================
# Written after COMMAND_WRITE_SPECIAL. Table 15 on document page 21 gives the
# label for each: " " (20H) sets the time of day as four ASCII digits in HhMm
# order, "&" (26H) sets the day of week, and "'" (27H) selects the 12 or 24 hour
# display format.

CMD_SET_TIME = b" "
CMD_SET_DAY_OF_WEEK = b"&"
CMD_SET_TIME_FORMAT = b"'"

# ==========================================================================
# Display position
# ==========================================================================
# The first byte of a TEXT file's mode field, from Table 12 on document page 18.
# It sets where on a multi-line sign the text begins.
#
# The document also lists "1" (31H) Left and "2" (32H) Right, both marked Alpha
# 3.0 protocol only. A BetaBrite speaks Alpha 1.0, so neither is defined here.

TEXT_POS_MIDDLE = b" "
TEXT_POS_TOP = b"\""
TEXT_POS_BOTTOM = b"&"
TEXT_POS_FILL = b"0"

# ==========================================================================
# Standard modes
# ==========================================================================
# Table 65, document page 89. The mode code is the second byte of the mode
# field and decides how a message arrives on the display.
#
# Two entries of that table are absent here. EXPLODE (75H) and CLOCK (76H) are
# both marked Alpha 3.0 protocol, which this sign does not speak. The reserved
# code 64H is absent for the same reason it is reserved.

MODE_ROTATE = b"a"
MODE_HOLD = b"b"
MODE_FLASH = b"c"
MODE_ROLLUP = b"e"
MODE_ROLLDOWN = b"f"
MODE_ROLLLEFT = b"g"
MODE_ROLLRIGHT = b"h"
MODE_WIPEUP = b"i"
MODE_WIPEDOWN = b"j"
MODE_WIPELEFT = b"k"
MODE_WIPERIGHT = b"l"
MODE_SCROLL = b"m"
MODE_AUTO = b"o"
MODE_ROLLIN = b"p"
MODE_ROLLOUT = b"q"
MODE_WIPEIN = b"r"
MODE_WIPEOUT = b"s"
MODE_CMPRSROT = b"t"

# ==========================================================================
# Special modes and special graphics
# ==========================================================================
# Tables 66 and 67, document pages 89 and 90. Each is the SPECIAL mode code
# "n" (6EH) followed by a specifier byte, so every constant here is two bytes.
#
# Table 66 prints its specifiers as a run: TWINKLE "0" 30H through SLOT MACHINE
# "9" 39H, then NEWS FLASH "A" and TRUMPET "B". Against those last two it prints
# the hex codes 3AH and 3BH, which do not agree with the characters beside them,
# since "A" is 41H and "B" is 42H. The characters are taken as authoritative for
# three reasons: the same table's CYCLE COLORS row prints "C" 43H, which is
# consistent; the specifiers of Table 67 are plainly characters rather than a
# continued hex run; and the colour list on page 82 makes the same jump from "9"
# (39H) to "A" (41H) with no such contradiction. The reading is that whoever set
# Table 66 continued the 30H to 39H run by hand for two rows.
#
# The sign's own names for two of these differ from the generic ones. Table 67
# lists 57H as "RUNNING ANIMAL or FISH ANIMATION" and 59H as "TURBO CAR or
# BALLOON ANIMATION", the second name in each pair being the BetaBrite's. The
# BetaBrite name is the one kept.

MODE_TWINKLE = b"n0"
MODE_SPARKLE = b"n1"
MODE_SNOW = b"n2"
MODE_INTERLOCK = b"n3"
MODE_SWITCH = b"n4"
MODE_SLIDE = b"n5"
MODE_SPRAY = b"n6"
MODE_STARBURST = b"n7"
MODE_WELCOME = b"n8"
MODE_SLOTMACHINE = b"n9"
MODE_NEWSFLASH = b"nA"
MODE_TRUMPET = b"nB"
MODE_THANKYOU = b"nS"
MODE_NOSMOKING = b"nU"
MODE_DRINKDRIVE = b"nV"
MODE_FISH = b"nW"
MODE_FIREWORKS = b"nX"
MODE_BALLOONS = b"nY"
MODE_CHERRYBOMB = b"nZ"

# ==========================================================================
# Control codes written inline in a message
# ==========================================================================
# Appendix G, "Control codes (00 - 1FH)", document pages 81 and 82. These are
# written into the ASCII message itself rather than into the mode field.
#
# The two-byte forms are a control code plus a selector, which the document gives
# as, for example, "05H + \"1\" (31H) = Double height on".

DBL_HEIGHT_CHARS_ON = b"\x05\x31"
DBL_HEIGHT_CHARS_OFF = b"\x05\x30"
TRUE_DESCENDERS_ON = b"\x06\x31"
TRUE_DESCENDERS_OFF = b"\x06\x30"
CHAR_FLASH_ON = b"\x07\x31"
CHAR_FLASH_OFF = b"\x07\x30"
NO_HOLD_SPEED = b"\x09"
LF = b"\x0a"
NEW_PAGE = b"\x0c"
CR = b"\x0d"
STRING_FILE_INSERT = b"\x10"
WIDE_CHARS_OFF = b"\x11"
WIDE_CHARS_ON = b"\x12"
CURTIME_INSERT = b"\x13"
DOTS_INSERT = b"\x14"
ALPHA_DOTS_INSERT = b"\x1f"
FIXED_WIDTH_OFF = b"\x1e\x30"
FIXED_WIDTH_ON = b"\x1e\x31"

# ==========================================================================
# Display speed
# ==========================================================================
# Appendix G, document page 81, control codes 15H to 19H. Speed 1 is the
# slowest and speed 5 the fastest.

SPEED_1 = b"\x15"
SPEED_2 = b"\x16"
SPEED_3 = b"\x17"
SPEED_4 = b"\x18"
SPEED_5 = b"\x19"

# ==========================================================================
# Temperature inserts
# ==========================================================================
# Appendix G, document page 81, under control code 08H. The document notes
# these work "only on Solar, 790i, 460i, 440i, and 430i" signs, so a BetaBrite is
# not expected to render them.

TEMP_CELSIUS = b"\x08\x1c"
TEMP_FAHRENHEIT = b"\x08\x1d"

# ==========================================================================
# Date inserts
# ==========================================================================
# Appendix G, document page 81, under control code 0BH, which the document
# introduces as "Call date (2-byte format)". The suffix names the separator the
# sign draws.
#
# The last two are not separators: 0BH + "8" (38H) is the MMM.DD, YYYY form, and
# 0BH + "9" (39H) gives the day of the week.

CURDATE_MMDDYY_SLASH = b"\x0b\x30"
CURDATE_DDMMYY_SLASH = b"\x0b\x31"
CURDATE_MMDDYY_DASH = b"\x0b\x32"
CURDATE_DDMMYY_DASH = b"\x0b\x33"
CURDATE_MMDDYY_DOT = b"\x0b\x34"
CURDATE_DDMMYY_DOT = b"\x0b\x35"
CURDATE_MMDDYY_SPACE = b"\x0b\x36"
CURDATE_DDMMYY_SPACE = b"\x0b\x37"
CURDATE_MMMDDYYYY = b"\x0b\x38"
CURDATE_WEEKDAYY = b"\x0b\x39"

# ==========================================================================
# Counters
# ==========================================================================
# Document page 87, in the "Counters" section that follows the extended
# character table, control code 08H with offsets 7AH to 7EH. Each inserts the
# value of one of the sign's five counters. They sit above the extended
# character range, whose offsets end at 61H.

COUNTER_1 = b"\x08\x7a"
COUNTER_2 = b"\x08\x7b"
COUNTER_3 = b"\x08\x7c"
COUNTER_4 = b"\x08\x7d"
COUNTER_5 = b"\x08\x7e"

# ==========================================================================
# Character sets
# ==========================================================================
# Appendix G, document page 82, control code 1AH, "Select character set", which
# the document gives as a two-byte form.
#
# The control code is quoted from the document. The selector byte of each entry
# below is not: the document draws that list as a graphic rather than setting it
# as text, so it survives neither extraction nor a search. These six therefore
# carry the same caveat as the extended character identities, and nothing in the
# suite pins them. They are used only by the simulator's decoder, which names
# whichever entry it matches, so a wrong selector here would mislabel a
# transmission rather than send a wrong byte to a sign.

CHARSET_5_NORMAL = b"\x1a\x31"
CHARSET_7_NORMAL = b"\x1a\x33"
CHARSET_7_FANCY = b"\x1a\x35"
CHARSET_10_NORMAL = b"\x1a\x36"
CHARSET_FULL_FANCY = b"\x1a\x38"
CHARSET_FULL_NORMAL = b"\x1a\x39"

# ==========================================================================
# Colours
# ==========================================================================
# Appendix G, document page 82, control code 1CH, "Select character color". The
# document adds that "some signs do not support all the following colors".
#
# The run is worth noting because it is where an off-by-one lands: red through
# rainbow 1 are the characters "1" to "9" (31H to 39H), and then it jumps to the
# letters, rainbow 2 through automatic being "A" to "C" (41H to 43H). There is no
# 3AH entry.

TEXT_COLOR_RED = b"\x1c\x31"
TEXT_COLOR_GREEN = b"\x1c\x32"
TEXT_COLOR_AMBER = b"\x1c\x33"
TEXT_COLOR_DIMRED = b"\x1c\x34"
TEXT_COLOR_DIMGREEN = b"\x1c\x35"
TEXT_COLOR_BROWN = b"\x1c\x36"
TEXT_COLOR_ORANGE = b"\x1c\x37"
TEXT_COLOR_YELLOW = b"\x1c\x38"
TEXT_COLOR_RAINBOW1 = b"\x1c\x39"
TEXT_COLOR_RAINBOW2 = b"\x1c\x41"
TEXT_COLOR_MIX = b"\x1c\x42"
TEXT_COLOR_AUTO = b"\x1c\x43"

# ==========================================================================
# Character attributes
# ==========================================================================
# Appendix G, document page 82, control code 1DH, "Select character attribute
# (3-byte format)". The document's own description of the shape: "1st byte is
# control code; 2nd byte is the attribute; and 3rd byte specifies either ON [\"1\"
# (31H)] or OFF [\"0\" (30H)]". OFF is the default for every one of them.
#
# Two attributes the document lists are absent here, both because they name
# hardware this is not: 36H auxiliary port, "Series 4000 & 7000 signs only", and
# 37H shadow characters, "Betabrite model 1036 and AlphaPremiere 9000 signs
# only".

CHAR_ATTRIB_WIDE_ON = b"\x1d\x30\x31"
CHAR_ATTRIB_WIDE_OFF = b"\x1d\x30\x30"
CHAR_ATTRIB_DBLW_ON = b"\x1d\x31\x31"
CHAR_ATTRIB_DBLW_OFF = b"\x1d\x31\x30"
CHAR_ATTRIB_DBLH_ON = b"\x1d\x32\x31"
CHAR_ATTRIB_DBLH_OFF = b"\x1d\x32\x30"
CHAR_ATTRIB_DESC_ON = b"\x1d\x33\x31"
CHAR_ATTRIB_DESC_OFF = b"\x1d\x33\x30"
CHAR_ATTRIB_FIX_ON = b"\x1d\x34\x31"
CHAR_ATTRIB_FIX_OFF = b"\x1d\x34\x30"
CHAR_ATTRIB_FNCY_ON = b"\x1d\x35\x31"
CHAR_ATTRIB_FNCY_OFF = b"\x1d\x35\x30"

# ==========================================================================
# Extended characters, as a control code and an offset
# ==========================================================================
# Document pages 84 to 86, "Extended character set (80 - C1H)". The document
# gives the whole set as a table of three columns: a code from 80H to C1H, the
# character itself, and the control code combination that produces it, which is
# always "08H + Offset" with the offset running from 20H to 61H.
#
# So the two forms are one subtraction apart: the character at code 80H + n is
# written 08H + (20H + n). The pairing is checked in
# ``tests/test_constant_values.py``, which asserts the two halves of this table
# address exactly the same characters.
#
# The document also notes that this set "is not available with the 5-high
# character set".
#
# What cannot be established from the document is which mark each code actually
# draws. The character column is drawn as vector outlines rather than set as
# text, so it survives neither text extraction nor a search. The names below are
# therefore the one part of this module not backed by a citation; see
# ``test_extended_character_identities_are_not_verified_here``, which says so in
# the suite rather than leaving it to be discovered.

XC_C_TAIL = b"\x08\x20"
XC_u_UMLAUT = b"\x08\x21"
XC_e_ACCENT = b"\x08\x22"
XC_a_CIRCUMFLEX = b"\x08\x23"
XC_a_UMLAUT = b"\x08\x24"
XC_a_GRAVE = b"\x08\x25"
XC_a_CIRCLE = b"\x08\x26"
XC_c_TAIL = b"\x08\x27"
XC_e_CIRCUMFLEX = b"\x08\x28"
XC_e_UMLAUT = b"\x08\x29"
XC_e_GRAVE = b"\x08\x2a"
XC_i_UMLAUT = b"\x08\x2b"
XC_i_CIRCUMFLEX = b"\x08\x2c"
XC_i_GRAVE = b"\x08\x2d"
XC_A_UMLAUT = b"\x08\x2e"
XC_A_CIRCLE = b"\x08\x2f"
XC_E_ACCENT = b"\x08\x30"
XC_ae_LIGATURE = b"\x08\x31"
XC_AE_LIGATURE = b"\x08\x32"
XC_o_CIRCUMFLEX = b"\x08\x33"
XC_o_UMLAUT = b"\x08\x34"
XC_o_GRAVE = b"\x08\x35"
XC_u_CIRCUMFLEX = b"\x08\x36"
XC_u_GRAVE = b"\x08\x37"
XC_y_UMLAUT = b"\x08\x38"
XC_O_UMLAUT = b"\x08\x39"
XC_U_UMLAUT = b"\x08\x3a"
XC_CENTS = b"\x08\x3b"
XC_POUNDS = b"\x08\x3c"
XC_YEN = b"\x08\x3d"
XC_PERCENT = b"\x08\x3e"
XC_SLANT_F = b"\x08\x3f"
XC_a_ACCENT = b"\x08\x40"
XC_i_ACCENT = b"\x08\x41"
XC_o_ACCENT = b"\x08\x42"
XC_u_ACCENT = b"\x08\x43"
XC_n_TILDE = b"\x08\x44"
XC_N_TILDE = b"\x08\x45"
XC_SUPER_a = b"\x08\x46"
XC_SUPER_o = b"\x08\x47"
XC_INVERT_QUESTION = b"\x08\x48"
XC_DEGREES = b"\x08\x49"
XC_INVERT_EXCLAIM = b"\x08\x4a"
XC_SINGLE_COL_SPACE = b"\x08\x4b"
XC_theta = b"\x08\x4c"
XC_THETA = b"\x08\x4d"
XC_c_ACCENT = b"\x08\x4e"
XC_C_ACCENT = b"\x08\x4f"
XC_c = b"\x08\x50"
XC_C = b"\x08\x51"
XC_d = b"\x08\x52"
XC_D = b"\x08\x53"
XC_s = b"\x08\x54"
XC_z = b"\x08\x55"
XC_Z = b"\x08\x56"
XC_BETA = b"\x08\x57"
XC_S = b"\x08\x58"
XC_BETA2 = b"\x08\x59"
XC_A_ACCENT = b"\x08\x5a"
XC_A_GRAVE = b"\x08\x5b"
XC_A_2ACCENT = b"\x08\x5c"
XC_a_2ACCENT = b"\x08\x5d"
XC_E_CAP = b"\x08\x5e"
XC_I_ACCENT = b"\x08\x5f"
XC_O_TILDE = b"\x08\x60"
XC_o_TILDE = b"\x08\x61"

# ==========================================================================
# Extended characters, as a single byte
# ==========================================================================
# The same characters as the section above, addressed by their own code from
# 80H to C1H rather than by the control code combination. Same document pages,
# same caveat about the identities.
#
# Where a mark has both cases, the two are named for the case they draw, so
# ``A_UMLAUT`` and ``a_UMLAUT`` are different codes rather than two spellings of
# one.
#
# ``TILDE`` (7EH) and ``BLOCK_CHAR`` (7FH) sit just below the extended range and
# are ordinary members of the standard set on document page 83.

TILDE = b"~"
BLOCK_CHAR = b"\x7f"
C_TAIL = b"\x80"
u_UMLAUT = b"\x81"
e_ACCENT = b"\x82"
a_CIRCUMFLEX = b"\x83"
a_UMLAUT = b"\x84"
a_GRAVE = b"\x85"
a_CIRCLE = b"\x86"
c_TAIL = b"\x87"
e_CIRCUMFLEX = b"\x88"
e_UMLAUT = b"\x89"
e_GRAVE = b"\x8a"
i_UMLAUT = b"\x8b"
i_CIRCUMFLEX = b"\x8c"
i_GRAVE = b"\x8d"
A_UMLAUT = b"\x8e"
A_CIRCLE = b"\x8f"
E_ACCENT = b"\x90"
ae_LIGATURE = b"\x91"
AE_LIGATURE = b"\x92"
o_CIRCUMFLEX = b"\x93"
o_UMLAUT = b"\x94"
o_GRAVE = b"\x95"
u_CIRCUMFLEX = b"\x96"
u_GRAVE = b"\x97"
y_UMLAUT = b"\x98"
O_UMLAUT = b"\x99"
U_UMLAUT = b"\x9a"
CENTS = b"\x9b"
POUNDS = b"\x9c"
YEN = b"\x9d"
PERCENT = b"\x9e"
SLANT_F = b"\x9f"
a_ACCENT = b"\xa0"
i_ACCENT = b"\xa1"
o_ACCENT = b"\xa2"
u_ACCENT = b"\xa3"
n_TILDE = b"\xa4"
N_TILDE = b"\xa5"
SUPER_a = b"\xa6"
SUPER_o = b"\xa7"
INVERT_QUESTION = b"\xa8"
DEGREES = b"\xa9"
INVERT_EXCLAIM = b"\xaa"
SINGLE_COL_SPACE = b"\xab"
theta = b"\xac"
THETA = b"\xad"
c_ACCENT = b"\xae"
C_ACCENT = b"\xaf"
CHAR_c = b"\xb0"
CHAR_C = b"\xb1"
CHAR_d = b"\xb2"
CHAR_D = b"\xb3"
CHAR_s = b"\xb4"
CHAR_z = b"\xb5"
CHAR_Z = b"\xb6"
BETA = b"\xb7"
CHAR_S = b"\xb8"
BETA2 = b"\xb9"
A_ACCENT = b"\xba"
A_GRAVE = b"\xbb"
A_2ACCENT = b"\xbc"
a_2ACCENT = b"\xbd"
E_ACCENT_HAT = b"\xbe"
I_ACCENT = b"\xbf"
O_TILDE = b"\xc0"
o_TILDE = b"\xc1"

# ==========================================================================
# Memory configuration
# ==========================================================================
# Special function label "$" (24H), written after COMMAND_WRITE_SPECIAL, so the
# payload begins "E$". Table 15 on document page 21 gives the entry format as
# FTPSIZEQQQQ, eleven characters per file.
#
# The one dangerous command in this module. The document: "whenever a Memory
# Configuration is written, the previous table is overwritten". Everything on the
# sign is lost. The service therefore allocates its whole pool once and
# reconfigures only when the plan itself changes.
#
# ``FILE_TYPE_DOTS`` is ``D``. Table 15 prints "D" 43H for it, which contradicts
# itself, since 43H is the character ``C`` and the two file types above it follow
# the characters rather than the hex ("A" 41H for TEXT, "B" 42H for STRING). The
# character is taken as authoritative.

SF_SET_MEMORY_CONFIG = b"$"
SF_MEMORY_POOL_SIZE = b"#"
FILE_TYPE_TEXT = b"A"
FILE_TYPE_STRING = b"B"
FILE_TYPE_DOTS = b"D"
FILE_LOCKED = b"L"
FILE_UNLOCKED = b"U"
TEXT_SCHEDULE_ALWAYS = b"FFFF"
FILE_OVERHEAD_BYTES = 11

# ==========================================================================
# Run sequence
# ==========================================================================
# Special function label "." (2EH), so the payload begins "E." and continues
# KPF: one mode byte, one lock byte, then the file labels to play in order.
# Table 15, document page 21.
#
# The document's own note on a stale label is what makes rewriting the sequence
# safe while slots come and go: "If a File Label is invalid or does not exist,
# the next File Label will be processed".

SF_SET_RUN_SEQUENCE = b"."
SF_RUN_TIME_TABLE = b")"
RUN_SEQ_BY_TIME = b"T"
RUN_SEQ_IGNORE_TIME = b"S"
RUN_SEQ_DELETE_AT_STOP = b"D"

# ==========================================================================
# File labels
# ==========================================================================
# Appendix A, "Valid File Labels", document page 50, which allows any printable
# character from 20H to 7EH.
#
# The priority TEXT file is "0" (30H). The document: "A Priority TEXT file is a
# special 125-byte message that does not need to be configured because it always
# exists on a sign", and writing to it stops every other TEXT file from being
# displayed.
#
# This service hands out "A" to "Z" only. A label a person can read in a log line
# is worth more than the extra capacity, and the real ceiling is the memory pool
# in bytes rather than a count of files. Two ranges are kept back: "0", the
# priority file, and "1" to "5", which become reserved target files if the sign's
# counter feature is ever switched on.

FILE_PRIORITY = b"0"
PRIORITY_FILE_CAPACITY = 125
TEXT_FILE_LABELS = (
    b"A",
    b"B",
    b"C",
    b"D",
    b"E",
    b"F",
    b"G",
    b"H",
    b"I",
    b"J",
    b"K",
    b"L",
    b"M",
    b"N",
    b"O",
    b"P",
    b"Q",
    b"R",
    b"S",
    b"T",
    b"U",
    b"V",
    b"W",
    b"X",
    b"Y",
    b"Z",
)
RESERVED_FILE_LABELS = (
    b"0",
    b"1",
    b"2",
    b"3",
    b"4",
    b"5",
)

# ==========================================================================
# What this sign will accept
# ==========================================================================
# The compatibility matrix lists the BetaBrite as EZ KEY II and Alpha 1.0 only,
# so nothing marked Alpha 2.0 or 3.0 may be used. That rules out the "E$$$$"
# clear-memory-and-compact-flash command, programmable sounds, and the ACK/NAK
# response feature, along with the display positions and modes noted absent
# above.
#
# The timing is the document's own: the inter-byte timeout for a standard packet
# is one second. The service's ``inter_packet_delay`` setting is a separate thing
# and deliberately conservative until it is measured against the sign.

PROTOCOL_GENERATION = '1.0'
INTER_BYTE_TIMEOUT_SECONDS = 1.0
