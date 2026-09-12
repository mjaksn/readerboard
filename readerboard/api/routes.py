"""The API.

Status codes mean what they say here: 400 for a message or value the sign cannot
render, 401 for a missing key, 404 for a slot or variable that does not exist,
409 when a pool is full or a variable still in use is deleted, and 503 when the
sign is unreachable. Which exception means which lives in
``readerboard.api.errors``, and these routes read no part of that table
themselves: they let the exception through and the handler registered from it
turns the failure into a status code and a ``detail`` body.

The paths carry no version prefix. They carried ``/v2`` while a second, older
surface stood beside them, and lost it when that surface was removed: a prefix
distinguishing one surface from nothing is a word every caller writes and no
reader learns anything from.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from readerboard.api.deps import AlertsDep, ClockDep, ControllerDep, RegistryDep, RequireApiKey
from readerboard.api.models import (
    AlertRequest,
    AlertResponse,
    ClockResponse,
    ControlCommandRequest,
    MessageRequest,
    SignInformationResponse,
    SlotActiveRequest,
    SlotKey,
    SlotResponse,
    TokenInfo,
    VariableName,
    VariableRequest,
    VariableResponse,
)
from readerboard.protocol import frames
from readerboard.protocol.markup import VALUE_TOKENS
from readerboard.protocol.replies import parse_general_information
from readerboard.protocol.tokens import (
    CONTROL_COMMANDS,
    DISPLAY_MODES,
    MARKUP_TOKENS,
    Token,
)
from readerboard.services import commands
from readerboard.services.registry import MessageRegistry
from readerboard.sign.state import VariableState

router = APIRouter()

messages = APIRouter(prefix="/messages", tags=["Messages"])
variables = APIRouter(prefix="/variables", tags=["Variables"])
alerts_routes = APIRouter(prefix="/alerts", tags=["Alerts"])
sign_routes = APIRouter(prefix="/sign", tags=["Sign"])
enumerations = APIRouter(prefix="/enumerations", tags=["Enumerations"])


# ===========================================================================
# Messages
# ===========================================================================


@messages.get("", summary="List the messages sharing the sign")
async def list_messages(registry: RegistryDep) -> list[SlotResponse]:
    """Return every registered slot, in rotation order, hidden ones included.

    A hidden slot is listed like any other, with `active` false. It is still
    registered and still holding its file; it is simply not one the sign plays.
    """
    return [SlotResponse.of(slot) for slot in registry.list_slots()]


@messages.get("/{key}", summary="Read one message")
async def get_message(key: SlotKey, registry: RegistryDep) -> SlotResponse:
    """Return one slot by name."""
    return SlotResponse.of(registry.get(key))


@messages.put("/{key}", summary="Register or replace a message", dependencies=[RequireApiKey])
async def put_message(
    key: SlotKey, body: MessageRequest, registry: RegistryDep
) -> SlotResponse:
    """Put a message in a slot, replacing whatever was there.

    The sign rotates through the slots that are showing on its own, so
    registering a second message does not displace the first.

    Leave `active` out and a hidden message stays hidden, which is what a source
    re-sending the same content every few minutes wants: repeating itself cannot
    switch back on something that was deliberately hidden. Send it and the message
    moves, so one call can write the text, set a deadline and put it up.
    `PUT /messages/{key}/active` does the same without resending the message.
    """
    slot = await registry.upsert(
        key,
        body.message,
        mode=body.display_mode,
        order=body.order,
        ttl_seconds=body.ttl_seconds,
        delete_on_expiry=body.delete_on_expiry,
        active=body.active,
        source=body.source,
    )
    return SlotResponse.of(slot)


@messages.put(
    "/{key}/active",
    summary="Show or hide a message without unregistering it",
    dependencies=[RequireApiKey],
)
async def set_message_active(
    key: SlotKey, body: SlotActiveRequest, registry: RegistryDep
) -> SlotResponse:
    """Take a message off the display, or put it back, keeping its slot either way.

    A hidden message stays registered. It keeps its slot, its file, its text and
    its place in the order, and is simply left out of the rotation the sign
    cycles, so showing it again needs no copy of what it said. `PUT /messages/{key}`
    can move it too, by sending `active`; this endpoint is for when the caller
    does not have the message text to resend, and a caller who omits `active`
    there cannot move it by accident.

    Hiding or showing one is a single run sequence write. That does disturb the
    display, but far less than rewriting a message does: briefly enough to be
    missed unless you are watching a static screen for it. The hidden message's
    own file keeps its text, so showing it again sends nothing but the sequence.
    The exception is the last message showing: that one's file is emptied as it
    goes, because a sign whose sequence names nothing freezes on what it was
    drawing.

    404 when no slot by that name is registered.
    """
    slot = await registry.set_active(key, body.active)
    return SlotResponse.of(slot)


@messages.delete(
    "/{key}",
    summary="Take a message off the sign",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def delete_message(key: SlotKey, registry: RegistryDep) -> Response:
    """Remove one slot and free the sign file it held."""
    await registry.remove(key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@messages.delete(
    "",
    summary="Take every message off the sign",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def clear_messages(registry: RegistryDep) -> Response:
    """Remove every slot, leaving the sign showing nothing."""
    await registry.clear()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Variables
# ===========================================================================


@variables.get("", summary="List the variables messages can call")
async def list_variables(registry: RegistryDep) -> list[VariableResponse]:
    """Return every variable, by name, with what calls each one."""
    return [_variable_response(registry, variable) for variable in registry.list_variables()]


@variables.get("/{name}", summary="Read one variable")
async def get_variable(name: VariableName, registry: RegistryDep) -> VariableResponse:
    """Return one variable by name."""
    return _variable_response(registry, registry.get_variable(name))


@variables.put("/{name}", summary="Create or change a variable", dependencies=[RequireApiKey])
async def put_variable(
    name: VariableName, body: VariableRequest, registry: RegistryDep
) -> VariableResponse:
    """Set a variable's value, creating the variable if it does not exist yet.

    A message calls a variable with `<var:name>`, and every message calling it
    shows the new value the next time the sign draws it. Only the variable's own
    small file is written, so unlike a change to a message this does not blank
    the display or restart the message: a scrolling message picks the new value
    up on its next pass. That makes a variable the way to show something that
    changes often, such as a temperature or a count.

    Formatting in the value carries on after the call, so a value of `<red>DOWN`
    turns the rest of the message red as well. To keep a changing number from
    shifting the text around it, put `<fixed_width>` in the message before the
    call and send values of the same length.

    409 when every variable is in use, and 400 when variables are switched off or
    the value does not fit. A value that does not fit is refused rather than cut
    short, because the sign does not cut it short either: it empties it.
    """
    variable = await registry.put_variable(
        name,
        body.value,
        ttl_seconds=body.ttl_seconds,
        stale_value=body.stale_value,
        source=body.source,
    )
    return _variable_response(registry, variable)


@variables.delete(
    "/{name}",
    summary="Delete a variable",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def delete_variable(name: VariableName, registry: RegistryDep) -> Response:
    """Delete a variable and free the sign file it held.

    409 while any message or the alert still calls it, naming what does. Its
    file is written into each of those messages, so deleting it would leave them
    calling a file the next variable could be given, and showing that variable's
    value.
    """
    await registry.remove_variable(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _variable_response(registry: MessageRegistry, variable: VariableState) -> VariableResponse:
    """Describe a variable along with everything that calls it."""
    return VariableResponse.of(
        variable,
        registry.callers(variable.name),
        called_by_alert=registry.alert_calls(variable.name),
    )


# ===========================================================================
# Alerts
# ===========================================================================


@alerts_routes.get("", summary="Read the alert holding the sign")
async def get_alert(alerts: AlertsDep) -> AlertResponse | None:
    """Return the active alert, or null if the sign is rotating normally."""
    alert = alerts.active
    return AlertResponse.of(alert) if alert else None


@alerts_routes.post("", summary="Take the sign over with an alert", dependencies=[RequireApiKey])
async def post_alert(body: AlertRequest, alerts: AlertsDep) -> AlertResponse:
    """Take the whole sign over until the alert is released.

    This uses the sign's priority file, which suppresses every other message. If
    a ttl is given, the sign is released automatically and the rotation resumes
    by itself.

    An alert can call variables with `<var:name>`, and a change to one shows on
    the alert without restarting it, as it does in a message. A variable the
    alert calls cannot be deleted until the alert is released or replaced, and a
    call to a variable that does not exist is a 400.
    """
    alert = await alerts.raise_alert(
        body.message,
        mode=body.display_mode,
        ttl_seconds=body.ttl_seconds,
    )
    return AlertResponse.of(alert)


@alerts_routes.delete(
    "",
    summary="Give the sign back",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def delete_alert(alerts: AlertsDep) -> Response:
    """Release an alert so the rotation resumes."""
    await alerts.release()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# The sign itself
# ===========================================================================


@sign_routes.post("/sync-clock", summary="Set the sign's clock now", dependencies=[RequireApiKey])
async def sync_clock(clock: ClockDep) -> ClockResponse:
    """Set the sign's clock and day of week immediately.

    The service already does this at startup, hourly, and whenever the link to
    the sign comes back. This is for when you would rather not wait.

    The sign is set one minute ahead of the real time, on purpose. Set Time
    carries four digits and no seconds, so a sign told the current minute starts
    that minute over and then reads behind for the rest of it, by up to
    fifty-nine seconds, and never ahead. Leading by a minute moves that
    one-sided error to the side where the sign is at worst a minute fast and
    never slow.
    """
    return ClockResponse(synced_at=await clock.sync())


@sign_routes.get(
    "/information",
    summary="Ask the sign what it is and how it is doing",
    dependencies=[RequireApiKey],
)
async def sign_information(controller: ControllerDep) -> SignInformationResponse:
    """Read the sign's own account of itself.

    The firmware build and the month it was released, the sign's clock and
    whether it draws a 12 or 24 hour one, whether its speaker is enabled, and
    how much of its memory pool is free. The protocol document calls this "most
    useful as a source of troubleshooting information", which is a fair summary:
    nothing here changes anything.

    This is the only read in the service, so it is also the only place a silent
    sign is distinguishable from an unplugged one. A sign that does not answer
    within a few seconds is a 503, the same as a sign that cannot be written to,
    and so is a sign that starts answering and does not finish, or answers with
    something this cannot read: the reply is the whole of what the endpoint has,
    so one that is cut short or will not parse leaves it with nothing to report.

    Two fields are worth reading carefully. `speaker_enabled` is why `SOUND` can
    appear to do nothing: the protocol calls disabled the default, and a muted
    sign beeps silently. And `memory_free` is the pool the memory configuration
    draws on, so a slot capacity that will not fit shows up here before it shows
    up as a failed reallocation.
    """
    reply = await controller.read_special(frames.read_general_information())
    return SignInformationResponse.of(parse_general_information(reply))


@sign_routes.post(
    "/command",
    summary="Send a control command to the sign",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def send_command(body: ControlCommandRequest, controller: ControllerDep) -> Response:
    """Send one of the sign's own control commands.

    `SOFT_RESET` restarts the sign. It erases nothing, the sign comes back
    showing what it was showing, and it is the first thing to try on a sign that
    has stopped responding. The display is blank for a few seconds while it runs
    its power-up diagnostics, and this call waits that out before answering, so
    a 204 means the sign is listening again. Reach for `POST /sign/reboot` only
    when a soft reset is not enough: that one erases the sign and rebuilds it.
    """
    await controller.send_special(
        commands.build(body.command, body.parameter),
        settle_seconds=commands.quiet_seconds_after(body.command),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@sign_routes.post(
    "/reboot",
    summary="Reboot the sign to recover it, then restore the display",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def reboot_sign(registry: RegistryDep, alerts: AlertsDep) -> Response:
    """Reset the sign to recover it, then put everything back.

    **A recovery tool, not a routine operation, and it is disruptive.** It sends
    the memory clear that resets the sign, which erases every file on it and
    takes the display blank for several seconds while the sign restarts. The
    service then waits for the reset to finish and re-pushes every message and
    the run sequence from its own record, so the sign comes back showing what it
    was showing before rather than empty. Any active alert is re-asserted as
    part of that restore.

    Use it for a sign that has stopped responding to writes or is showing
    garbage, the wedged-decoder state a unit mounted out of reach can fall into
    from a stray bit and that cannot be fixed by power cycling it by hand. Do
    not use it to clear the sign: `DELETE /messages` takes every message off
    without resetting anything, and this puts them all straight back. Expect a
    blank display for twelve seconds or more before the rotation returns,
    longer with a lot of messages to put back.

    503 if the sign cannot be reached, since a sign that is not answering cannot
    be rebooted.
    """
    await registry.reboot()
    await alerts.reassert()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Enumerations
# ===========================================================================


def _as_info(tokens: tuple[Token, ...]) -> list[TokenInfo]:
    return [TokenInfo(name=token.text, description=token.description) for token in tokens]


# Not a token, since it carries a name, but it is written inline like one and a
# caller looking for what a message can say should find it here.
VARIABLE_CALL = TokenInfo(
    name="<var:name>",
    description=(
        "Show a variable's value here, with name replaced by the variable's name. The "
        "variable has to exist first, and formatting set in its value carries on after it"
    ),
)


@enumerations.get("/markup-tokens", summary="Markup tokens a message may contain")
async def markup_tokens() -> list[TokenInfo]:
    """List every token that can be written inline in a message, and the variable call."""
    return [*_as_info(MARKUP_TOKENS), VARIABLE_CALL]


@enumerations.get("/value-tokens", summary="Markup tokens a variable's value may contain")
async def value_tokens() -> list[TokenInfo]:
    """List the tokens a variable's value may contain.

    The message tokens, less `<week_day>`, which the sign draws as a literal
    character from inside a variable. A value cannot call another variable
    either, for the same reason.
    """
    return _as_info(VALUE_TOKENS)


@enumerations.get("/display-modes", summary="Ways the sign can present a message")
async def display_modes() -> list[TokenInfo]:
    """List every display mode."""
    return _as_info(DISPLAY_MODES)


@enumerations.get("/control-commands", summary="Commands aimed at the sign itself")
async def control_commands() -> list[TokenInfo]:
    """List every control command."""
    return _as_info(CONTROL_COMMANDS)


router.include_router(messages)
router.include_router(variables)
router.include_router(alerts_routes)
router.include_router(sign_routes)
router.include_router(enumerations)
