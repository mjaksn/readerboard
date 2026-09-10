"""The vocabulary a caller can use: markup tokens, display modes, control commands.

Every name here is part of the service's public surface, so renaming one is a
breaking change for whatever is already sending it. The table covers what the
sign can actually do rather than a convenient subset, because a mode missing
from here is a mode nobody can reach.
"""

from __future__ import annotations

from dataclasses import dataclass

from readerboard.protocol import constants as c


@dataclass(frozen=True, slots=True)
class Token:
    """One named thing a caller can ask for, and the bytes it turns into."""

    text: str
    value: bytes
    description: str


# ===========================================================================
# Markup tokens, written inline in a message as <name>.
# ===========================================================================

MARKUP_TOKENS: tuple[Token, ...] = (
    Token("<red>", c.TEXT_COLOR_RED, "Set text colour to red"),
    Token("<green>", c.TEXT_COLOR_GREEN, "Set text colour to green"),
    Token("<amber>", c.TEXT_COLOR_AMBER, "Set text colour to amber"),
    Token("<dimred>", c.TEXT_COLOR_DIMRED, "Set text colour to dim red"),
    Token("<dimgreen>", c.TEXT_COLOR_DIMGREEN, "Set text colour to dim green"),
    Token("<brown>", c.TEXT_COLOR_BROWN, "Set text colour to brown"),
    Token("<orange>", c.TEXT_COLOR_ORANGE, "Set text colour to orange"),
    Token("<yellow>", c.TEXT_COLOR_YELLOW, "Set text colour to yellow"),
    Token("<rainbow1>", c.TEXT_COLOR_RAINBOW1, "Colour the whole message as a rainbow"),
    Token("<rainbow2>", c.TEXT_COLOR_RAINBOW2, "Colour each character as a rainbow"),
    Token("<color_mix>", c.TEXT_COLOR_MIX, "Give each character a different colour"),
    Token("<color_auto>", c.TEXT_COLOR_AUTO, "Cycle through the colour modes"),
    Token("<flash_on>", c.CHAR_FLASH_ON, "Characters after this token flash"),
    Token("<flash_off>", c.CHAR_FLASH_OFF, "Characters after this token stop flashing"),
    # No wide token either, and for the same reason as double height rather
    # than a different one. The protocol's own "enable wide characters" (12H)
    # drew text identical to plain on the sign. What a person reads as wider
    # text here comes from the character set and the attributes below, which
    # were measured doing something.
    #
    # No double height token. A Betabrite is "always 7 dots (or pixels) high",
    # and on seven rows there is nothing for double height to do: both the 05H
    # form this used to offer and the 1DH+2 attribute rendered pixel-identical
    # to plain text on the sign. See docs/protocol-notes.md.
    #
    # These three are what a person actually sees on this hardware, out of the
    # twenty character sets and attributes the protocol offers. Named for their
    # appearance rather than for the document's labels, which contradict
    # themselves here: it calls 1AH+6 "ten high standard" and also "seven stroke
    # fancy", and on seven rows it is neither, it is ordinary text.
    #
    # The three font tokens are a choice rather than a switch, so returning from
    # one means selecting another; <font_normal> is the way back.
    Token("<font_normal>", c.CHARSET_7_NORMAL, "The ordinary character set, and the way back"),
    Token("<font_half_height>", c.CHARSET_5_NORMAL, "Short characters, five rows rather than seven"),
    Token("<font_wide>", c.CHARSET_7_FANCY, "Wider characters, full height"),
    Token("<bold_on>", c.CHAR_ATTRIB_WIDE_ON, "Characters after this token are bold"),
    Token("<bold_off>", c.CHAR_ATTRIB_WIDE_OFF, "Return to characters of ordinary weight"),
    Token("<extra_wide_on>", c.CHAR_ATTRIB_DBLW_ON, "Characters after this token are extra wide"),
    Token("<extra_wide_off>", c.CHAR_ATTRIB_DBLW_OFF, "Return to characters of ordinary width"),
    Token("<fixed_width>", c.FIXED_WIDTH_ON, "Left justify and make text fixed width; put this first"),
    Token("<proportional>", c.FIXED_WIDTH_OFF, "Return to proportionally spaced text"),
    Token("<degree>", c.XC_DEGREES, "Degree symbol"),
    Token("<block>", c.BLOCK_CHAR, "A solid square block character"),
    Token("<half_space>", c.TILDE, "A half width space"),
    Token("<time>", c.CURTIME_INSERT, "Insert the sign's current time"),
    Token("<week_day>", c.CURDATE_WEEKDAYY, "Insert the current day of the week"),
    # There is deliberately no token for the sign's date, and this is the one
    # place in the table where something the sign can draw is withheld.
    #
    # The sign stores a two-digit year. The windowing that would read "26" as
    # 2026 is gated by Table 15's footnote 15 to "Alpha protocol version 2.0 and
    # greater", and Table 3 lists a Betabrite as EZ KEY II and Alpha 1.0 only.
    # So this sign applies no century at all, and no amount of setting its date
    # makes it show the right one. A date token would render a confidently wrong
    # date, which is worse than offering nothing, and the whole point of the
    # strict renderer is that a caller is never shown what it did not ask for.
    #
    # The time and the day of week above are a different matter and stay: both
    # are registers of their own, and ClockService writes them at startup,
    # hourly, and on every reconnect, so they are right. The protocol's date
    # control codes are still named in constants.py, and the sign simulator
    # still annotates them, so a message stored by an older version remains
    # readable. See docs/protocol-notes.md.
    Token("<newline>", c.CR, "Start a new line"),
    Token("<new_page>", c.NEW_PAGE, "Start the next display page"),
    Token("<no_hold_speed>", c.NO_HOLD_SPEED, "Do not pause after the mode finishes"),
    Token("<speed1>", c.SPEED_1, "Set the scroll speed to 1, the slowest"),
    Token("<speed2>", c.SPEED_2, "Set the scroll speed to 2"),
    Token("<speed3>", c.SPEED_3, "Set the scroll speed to 3"),
    Token("<speed4>", c.SPEED_4, "Set the scroll speed to 4"),
    Token("<speed5>", c.SPEED_5, "Set the scroll speed to 5, the fastest"),
)


# ===========================================================================
# Display modes, chosen per message rather than written inline.
# ===========================================================================

DISPLAY_MODES: tuple[Token, ...] = (
    Token(
        "HOLD",
        c.MODE_HOLD,
        "Hold the message still, unless it is wider than the sign, in which case "
        "the sign scrolls it repeatedly",
    ),
    Token("FLASH", c.MODE_FLASH, "Hold the message still and flash it repeatedly"),
    Token("ROTATE", c.MODE_ROTATE, "Scroll the message across the sign repeatedly"),
    Token("ROLLUP", c.MODE_ROLLUP, "Push the old message up with the new one"),
    Token("ROLLDOWN", c.MODE_ROLLDOWN, "Push the old message down with the new one"),
    Token("ROLLLEFT", c.MODE_ROLLLEFT, "Push the old message left with the new one"),
    Token("ROLLRIGHT", c.MODE_ROLLRIGHT, "Push the old message right with the new one"),
    Token("WIPEUP", c.MODE_WIPEUP, "Wipe the new message up over the old one"),
    Token("WIPEDOWN", c.MODE_WIPEDOWN, "Wipe the new message down over the old one"),
    Token("WIPELEFT", c.MODE_WIPELEFT, "Wipe the new message left over the old one"),
    Token("WIPERIGHT", c.MODE_WIPERIGHT, "Wipe the new message right over the old one"),
    Token("ROLLIN", c.MODE_ROLLIN, "Push the new message inward"),
    Token("ROLLOUT", c.MODE_ROLLOUT, "Push the new message outward"),
    Token("WIPEIN", c.MODE_WIPEIN, "Wipe the new message inward over the old one"),
    Token("WIPEOUT", c.MODE_WIPEOUT, "Wipe the new message outward over the old one"),
    Token("COMPRESSED_ROTATE", c.MODE_CMPRSROT, "Scroll the message at half width"),
    Token("AUTO", c.MODE_AUTO, "Let the sign pick a mode at random"),
    Token("TWINKLE", c.MODE_TWINKLE, "Twinkle the message"),
    Token("SPARKLE", c.MODE_SPARKLE, "Sparkle the new message over the old one"),
    Token("SNOW", c.MODE_SNOW, "Snow the new message onto the sign"),
    Token("INTERLOCK", c.MODE_INTERLOCK, "Interlock the new message over the old one"),
    Token("SWITCH", c.MODE_SWITCH, "Switch the old message off and the new one on, character by character"),
    Token("SLIDE", c.MODE_SLIDE, "Slide characters in one at a time, right to left"),
    Token("SPRAY", c.MODE_SPRAY, "Spray the message on right to left"),
    Token("STARBURST", c.MODE_STARBURST, "Explode the new message onto the sign"),
    Token("WELCOME", c.MODE_WELCOME, "Display a script \"Welcome\""),
    Token("SLOTMACHINE", c.MODE_SLOTMACHINE, "Display slot machine reels"),
    Token("NEWSFLASH", c.MODE_NEWSFLASH, "Display a \"Newsflash\" animation"),
    Token("TRUMPET", c.MODE_TRUMPET, "Display a trumpet animation"),
    Token("THANKYOU", c.MODE_THANKYOU, "Display a script \"Thank You\""),
    Token("NOSMOKING", c.MODE_NOSMOKING, "Display a \"No Smoking\" animation"),
    Token("DRINKDRIVE", c.MODE_DRINKDRIVE, "Display a \"Don't Drink and Drive\" animation"),
    Token("FISH", c.MODE_FISH, "Display swimming fish"),
    Token("FIREWORKS", c.MODE_FIREWORKS, "Display a fireworks animation"),
    Token("BALLOONS", c.MODE_BALLOONS, "Display a balloon animation"),
    Token("CHERRYBOMB", c.MODE_CHERRYBOMB, "Display a cherry bomb animation"),
)


# ===========================================================================
# Control commands, sent on their own rather than as part of a message.
# ===========================================================================

CONTROL_COMMANDS: tuple[Token, ...] = (
    Token(
        "SET_TIME",
        c.CMD_SET_TIME,
        "Set the sign's internal clock. The parameter is the time as four ASCII digits, "
        "HHMM on a 24 hour clock",
    ),
    Token(
        "SET_TIME_FORMAT",
        c.CMD_SET_TIME_FORMAT,
        "Set how the sign renders the time. The parameter is 'S' for standard or 'M' for military",
    ),
    Token(
        "SET_DAY_OF_WEEK",
        c.CMD_SET_DAY_OF_WEEK,
        "Set the sign's day of the week. The parameter is a single ASCII digit, "
        "1 for Sunday through 7 for Saturday",
    ),
    Token(
        "SPEAKER",
        c.CMD_SPEAKER_ENABLE,
        "Enable or silence the sign's speaker. The parameter is 'ON' or 'OFF'. Turning it "
        "off mutes the sign: SOUND is still accepted and makes no noise. The setting "
        "lives on the sign and survives a restart",
    ),
    Token(
        "SOUND",
        c.CMD_SPEAKER_TONE,
        "Sound the sign's speaker. The parameter is 'TONE' for one continuous tone of "
        "about two seconds or 'BEEPS' for three short beeps. The sign has a fixed-pitch "
        "buzzer, so there is no pitch to choose and no other sound to make",
    ),
    Token(
        "SOFT_RESET",
        c.CMD_SOFT_RESET,
        "Restart the sign, which runs its power-up diagnostics and comes back showing "
        "what it was showing. Nothing is erased and no parameter is taken. Use it first "
        "on a sign that has stopped responding; the display is blank for a few seconds "
        "while it restarts",
    ),
)


# ===========================================================================
# Vertical position of text within a TEXT file.
# ===========================================================================

TEXT_POSITIONS: tuple[Token, ...] = (
    Token("MIDDLE", c.TEXT_POS_MIDDLE, "Centre the text vertically"),
    Token("TOP", c.TEXT_POS_TOP, "Begin the text at the top of the sign"),
    Token("BOTTOM", c.TEXT_POS_BOTTOM, "Place the text immediately below the top"),
    Token("FILL", c.TEXT_POS_FILL, "Centre vertically and use every available line"),
)


MARKUP_BY_TEXT: dict[str, Token] = {token.text: token for token in MARKUP_TOKENS}
MODE_BY_NAME: dict[str, Token] = {token.text: token for token in DISPLAY_MODES}
COMMAND_BY_NAME: dict[str, Token] = {token.text: token for token in CONTROL_COMMANDS}
POSITION_BY_NAME: dict[str, Token] = {token.text: token for token in TEXT_POSITIONS}
