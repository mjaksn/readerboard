"""Turning a named control command and its parameter into a payload.

These are the commands that act on the sign itself rather than on a message:
setting its clock, its day of week, and how it renders the time.

The set is deliberately closed. Anything reaching the sign from here is one of
these three, none of which touches the memory configuration or the run time
table, so a caller cannot use this route to disturb the file layout the service
believes it has.

Note the day of week numbering, which is easy to get wrong: the protocol defines
1 for Sunday through 7 for Saturday, not 0 through 6.
"""

from __future__ import annotations

from readerboard.protocol import frames
from readerboard.protocol.tokens import COMMAND_BY_NAME


class UnknownCommand(ValueError):
    """No control command by that name exists."""


class BadParameter(ValueError):
    """The parameter is not valid for the command it was given to."""


def build(name: str, parameter: str) -> bytes:
    """Build the payload for a named control command.

    ``name`` is matched case insensitively.
    """
    command = name.strip().upper()
    if command not in COMMAND_BY_NAME:
        raise UnknownCommand(
            "unrecognised control command %r; the available commands are %s"
            % (name, ", ".join(sorted(COMMAND_BY_NAME)))
        )

    if command == "SET_TIME":
        return _set_time(parameter)
    if command == "SET_DAY_OF_WEEK":
        return _set_day_of_week(parameter)
    if command == "SET_TIME_FORMAT":
        return _set_time_format(parameter)

    raise UnknownCommand("control command %r has no handler" % command)  # pragma: no cover


def _set_time(parameter: str) -> bytes:
    value = parameter.strip()
    if len(value) != 4 or not value.isdigit():
        raise BadParameter(
            "SET_TIME takes the time as four digits, HHMM on a 24 hour clock, got %r"
            % parameter
        )
    try:
        return frames.set_time(int(value[:2]), int(value[2:]))
    except frames.ProtocolError as err:
        raise BadParameter(str(err)) from err


def _set_day_of_week(parameter: str) -> bytes:
    value = parameter.strip()
    if len(value) != 1 or not value.isdigit():
        raise BadParameter(
            "SET_DAY_OF_WEEK takes a single digit, 1 for Sunday through 7 for Saturday, "
            "got %r" % parameter
        )
    try:
        return frames.set_day_of_week(int(value))
    except frames.ProtocolError as err:
        raise BadParameter(str(err)) from err


def _set_time_format(parameter: str) -> bytes:
    value = parameter.strip().upper()
    if value not in {"S", "M"}:
        raise BadParameter(
            "SET_TIME_FORMAT takes 'S' for standard or 'M' for military, got %r" % parameter
        )
    return frames.set_time_format(military=value == "M")
