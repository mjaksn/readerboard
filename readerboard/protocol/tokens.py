"""The vocabulary a caller can use: markup tokens, display modes, control commands.

Every name here is part of the service's public surface. The markup token texts
and the three original display mode names are what existing Home Assistant
payloads already send, so they are fixed; the rest of the table extends what the
sign has always been able to do but the old API never exposed.
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
    Token("<wide_on>", c.WIDE_CHARS_ON, "Characters after this token are wide"),
    Token("<wide_off>", c.WIDE_CHARS_OFF, "Characters after this token are normal width"),
    Token(
        "<dbl_height_on>",
        c.DBL_HEIGHT_CHARS_ON,
        "Characters after this token are double height",
    ),
    Token("<dbl_height_off>", c.DBL_HEIGHT_CHARS_OFF, "Return to single height characters"),
    Token("<fixed_width>", c.FIXED_WIDTH_ON, "Left justify and make text fixed width; put this first"),
    Token("<proportional>", c.FIXED_WIDTH_OFF, "Return to proportionally spaced text"),
    Token("<degree>", c.XC_DEGREES, "Degree symbol"),
    Token("<block>", c.BLOCK_CHAR, "A solid square block character"),
    Token("<half_space>", c.TILDE, "A half width space"),
    Token("<time>", c.CURTIME_INSERT, "Insert the sign's current time"),
    Token("<week_day>", c.CURDATE_WEEKDAYY, "Insert the current day of the week"),
    Token("<date>", c.CURDATE_MMDDYY_SLASH, "Insert the current date as MM/DD/YY"),
    Token("<date_dmy>", c.CURDATE_DDMMYY_SLASH, "Insert the current date as DD/MM/YY"),
    Token("<date_long>", c.CURDATE_MMMDDYYYY, "Insert the current date as MMM.DD, YYYY"),
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
        "Set the sign's internal clock. The parameter is the time as HHMM on a 24 hour clock",
    ),
    Token(
        "SET_TIME_FORMAT",
        c.CMD_SET_TIME_FORMAT,
        "Set how the sign renders the time. The parameter is 'S' for standard or 'M' for military",
    ),
    Token(
        "SET_DAY_OF_WEEK",
        c.CMD_SET_DAY_OF_WEEK,
        "Set the sign's day of the week. The parameter is a single digit, "
        "1 for Sunday through 7 for Saturday",
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


def describe(tokens: tuple[Token, ...], key: str) -> list[dict[str, str]]:
    """Render a token table as the list of dicts the enumeration endpoints return."""
    return [{key: token.text, "description": token.description} for token in tokens]
