"""Turning a named control command and its parameter into a payload.

These are the commands that act on the sign itself rather than on a message:
setting its clock, its day of week, how it renders the time, sounding and
silencing its speaker, and restarting it.

The set is deliberately closed. Anything reaching the sign from here is one of
these six, none of which touches the memory configuration or the run time
table, so a caller cannot use this route to disturb the file layout the service
believes it has.

SPEAKER and SOUND are kept apart on purpose. A sound command that enabled the
speaker on its way past would make SPEAKER OFF unable to hold, so muting is the
one thing that stays where the caller put it.

SOFT_RESET is in the set for exactly that reason. It restarts the sign and the
sign's memory survives, verified on hardware either side of the reset, so it
disturbs no file the service is tracking. The destructive reset, which erases
the sign, is not reachable from here and lives behind ``POST /sign/reboot``.

Note the day of week numbering, which is easy to get wrong: the protocol defines
1 for Sunday through 7 for Saturday, not 0 through 6.
"""

from __future__ import annotations

from readerboard.protocol import frames
from readerboard.protocol.tokens import COMMAND_BY_NAME
from readerboard.sign.controller import RESET_SETTLE_SECONDS, SOUND_SETTLE_SECONDS

# How long the sign cannot listen after each command. See
# :func:`quiet_seconds_after`. A command absent from this map costs nothing.
_QUIET_AFTER = {
    "SOFT_RESET": RESET_SETTLE_SECONDS,
    "SOUND": SOUND_SETTLE_SECONDS,
}


class UnknownCommand(ValueError):
    """No control command by that name exists."""


class BadParameter(ValueError):
    """The parameter is not valid for the command it was given to."""


def _is_digits(value: str) -> bool:
    """Whether ``value`` is digits this module can hand to ``int``, and only those.

    ``str.isdigit`` is the wrong question and answers it two ways. It is true of
    a superscript two, which ``int`` then refuses, so the guard passed and the
    conversion below raised an error nothing here catches. It is also true of
    the Arabic-Indic digits, which ``int`` does accept, so "١٢٣٤" was quietly
    read as a time. Neither is what a caller means by the four digits of a 24
    hour clock or the single digit of a day of the week, which are the two
    parameters this guards.
    """
    return value.isascii() and value.isdecimal()


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
    if command == "SPEAKER":
        return _speaker(parameter)
    if command == "SOUND":
        return _sound(parameter)
    if command == "SOFT_RESET":
        return _soft_reset(parameter)

    raise UnknownCommand("control command %r has no handler" % command)  # pragma: no cover


def quiet_seconds_after(name: str) -> float:
    """How long the sign cannot be written to once this command has been sent.

    Two commands leave it deaf and they do so for different reasons, which is
    why this asks about the effect rather than the cause. SOFT_RESET restarts
    the sign and it hears nothing through its power-up diagnostics. SOUND is not
    a reset at all, and the protocol still switches the serial port off for the
    length of the tone: "the tone generation command must be the last
    transmission frame because the sign's serial port is disabled (and cannot
    receive any data) while a tone is generated".

    A write sent into either window is not refused, it is simply not there
    afterwards, which is the quietest way for a message to go missing. The route
    holds the request open instead, so a 204 means the sign is listening again
    rather than that bytes were sent.
    """
    return _QUIET_AFTER.get(name.strip().upper(), 0.0)


def _set_time(parameter: str) -> bytes:
    value = parameter.strip()
    if len(value) != 4 or not _is_digits(value):
        raise BadParameter(
            "SET_TIME takes the time as four ASCII digits, HHMM on a 24 hour clock, got %r"
            % parameter
        )
    try:
        return frames.set_time(int(value[:2]), int(value[2:]))
    except frames.ProtocolError as err:
        raise BadParameter(str(err)) from err


def _set_day_of_week(parameter: str) -> bytes:
    value = parameter.strip()
    if len(value) != 1 or not _is_digits(value):
        raise BadParameter(
            "SET_DAY_OF_WEEK takes a single ASCII digit, 1 for Sunday through 7 for Saturday, "
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


def _speaker(parameter: str) -> bytes:
    value = parameter.strip().upper()
    if value == "ON":
        return frames.set_speaker(True)
    if value == "OFF":
        return frames.set_speaker(False)
    raise BadParameter("SPEAKER takes 'ON' or 'OFF', got %r" % parameter)


def _sound(parameter: str) -> bytes:
    # Named rather than passed through as the protocol's "0" and "1", the way
    # display modes are named. The sounds are spelled out
    # instead of taking the protocol's programmable form, because this sign's
    # buzzer ignores the frequency that form carries; see constants.py.
    value = parameter.strip().upper()
    if value == "TONE":
        return frames.sound_tone()
    if value == "BEEPS":
        return frames.sound_beeps()
    raise BadParameter(
        "SOUND takes 'TONE' for one continuous tone or 'BEEPS' for three short beeps, "
        "got %r" % parameter
    )


def _soft_reset(parameter: str) -> bytes:
    # The protocol is explicit that this field carries no data, so a parameter
    # is refused rather than ignored: a caller who passed one meant something by
    # it, and silently dropping it would leave them believing it took effect.
    if parameter.strip():
        raise BadParameter("SOFT_RESET takes no parameter, got %r" % parameter)
    return frames.soft_reset()
