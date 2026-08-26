"""A smaller surface for clients that would rather not read status codes.

Fixed paths, one message, and **every response is HTTP 200** with the outcome in
the body. That suits a Home Assistant ``rest_command`` or a shell one-liner in a
cron job, neither of which branches gracefully on a status code.

There is one exception to always-200, and it is deliberate: a request without a
valid API key gets a 401 like any other, because a caller the service will not
talk to is not the same as a request that failed.

``POST /Write/Message`` writes to a reserved slot rather than to the sign's
priority file. That distinction matters more than it looks. By protocol a
priority message suppresses every other file on the sign, so writing one here
would quietly turn a service that shares the sign between several sources into a
service that can only ever show one thing. Written to an ordinary slot it looks
identical while it is the only message registered, and it shares the sign the
moment anything else registers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from readerboard.api.deps import ControllerDep, RegistryDep, RequireApiKey, SettingsDep
from readerboard.api.models import (
    SimpleCommandRequest,
    SimpleControlCommand,
    SimpleDisplayMode,
    SimpleMessageRequest,
    SimpleResult,
    SimpleToken,
)
from readerboard.protocol.tokens import CONTROL_COMMANDS, DISPLAY_MODES, MARKUP_TOKENS
from readerboard.services import commands
from readerboard.services.registry import DEFAULT_SLOT_KEY

logger = logging.getLogger(__name__)

router = APIRouter()

write = APIRouter(prefix="/Write", tags=["Write (simple)"])
enumerations = APIRouter(prefix="/Enumerations", tags=["Enumerations (simple)"])


@write.post("/Message", summary="Write a message to the sign", dependencies=[RequireApiKey])
async def write_message(
    body: SimpleMessageRequest, registry: RegistryDep, settings: SettingsDep
) -> SimpleResult:
    """Display a message on the sign.

    The message goes into a reserved slot rather than the sign's priority file,
    so other sources can share the sign with it.
    """
    try:
        await registry.upsert(
            DEFAULT_SLOT_KEY,
            body.message,
            mode=body.display_mode.strip().upper(),
            position="MIDDLE",
            source="simple",
            # Unset by default, which is exactly how this endpoint has always
            # behaved: the message stays until something replaces it. Set it and
            # an automation that stops calling leaves an empty sign rather than
            # a stale temperature that still looks current.
            ttl_seconds=settings.default_slot_ttl_seconds,
            # Unknown tokens are passed through as literal text, exactly as the
            # old parser did, so a payload that worked before cannot start
            # failing now.
            strict=False,
        )
    except KeyError:
        return SimpleResult.error(
            "The display mode '%s' is not valid" % body.display_mode
        )
    except Exception as err:
        logger.warning("simple write failed: %s", err)
        return SimpleResult.error(str(err))

    return SimpleResult.ok("Message displayed on sign")


@write.post(
    "/ControlCommand",
    summary="Send a control command to the sign",
    dependencies=[RequireApiKey],
)
async def write_control_command(
    body: SimpleCommandRequest, controller: ControllerDep
) -> SimpleResult:
    """Send one of the sign's control commands."""
    try:
        payload = commands.build(body.command, body.parameter)
    except commands.UnknownCommand:
        # The old wording, kept because something may be matching on it.
        return SimpleResult.error("Unrecognized control command '%s'" % body.command)
    except commands.BadParameter as err:
        return SimpleResult.error(str(err))

    try:
        await controller.send_special(payload)
    except Exception as err:
        logger.warning("simple control command failed: %s", err)
        return SimpleResult.error(str(err))

    return SimpleResult.ok("Control command sent to sign")


@enumerations.get("/DisplayModes", summary="Available display modes")
async def display_modes() -> list[SimpleDisplayMode]:
    """List the display modes, in the old shape."""
    return [
        SimpleDisplayMode(display_mode=token.text, description=token.description)
        for token in DISPLAY_MODES
    ]


@enumerations.get("/ControlCommands", summary="Available control commands")
async def control_commands() -> list[SimpleControlCommand]:
    """List the control commands, in the old shape."""
    return [
        SimpleControlCommand(control_command=token.text, description=token.description)
        for token in CONTROL_COMMANDS
    ]


@enumerations.get("/MarkupTokens", summary="Available markup tokens")
async def markup_tokens() -> list[SimpleToken]:
    """List the markup tokens, in the old shape.

    ``token_text`` is the token to write in a message, such as ``<red>``, and
    ``description`` says what it does. Easy to describe the wrong way round,
    which is why they are spelled out here.
    """
    return [
        SimpleToken(token_text=token.text, description=token.description)
        for token in MARKUP_TOKENS
    ]


router.include_router(write)
router.include_router(enumerations)
