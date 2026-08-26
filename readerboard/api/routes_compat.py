"""The old API, kept working.

Home Assistant's rest_command and a crontab line already call these, and neither
should have to change for the rewrite. The request bodies, the response bodies
and the always-200 behaviour are all exactly as they were.

Two things did change, and both are deliberate.

The first is that ``POST /Write/Message`` no longer writes the sign's priority
file. It registers a reserved slot instead. Writing the priority file is what
made the old server a one-message service: by protocol, a priority message
suppresses every other file on the sign. Routed to an ordinary slot, the same
call produces the same visible result while it is the only message registered,
and coexists once something else registers too. Sending it to the priority file
would defeat the entire rotation feature.

The second is that these endpoints now need an API key. That is the single
exception to always-200: a request without the key gets a 401 like any other,
because a caller the service will not talk to is not the same as a request that
failed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from readerboard.api.deps import ControllerDep, RegistryDep, RequireApiKey, SettingsDep
from readerboard.api.models import (
    LegacyCommandRequest,
    LegacyControlCommand,
    LegacyDisplayMode,
    LegacyMessageRequest,
    LegacyResult,
    LegacyToken,
)
from readerboard.protocol.tokens import CONTROL_COMMANDS, DISPLAY_MODES, MARKUP_TOKENS
from readerboard.services import commands
from readerboard.services.registry import LEGACY_SLOT_KEY

logger = logging.getLogger(__name__)

router = APIRouter()

write = APIRouter(prefix="/Write", tags=["Write (compatibility)"])
enumerations = APIRouter(prefix="/Enumerations", tags=["Enumerations (compatibility)"])


@write.post("/Message", summary="Write a message to the sign", dependencies=[RequireApiKey])
async def write_message(
    body: LegacyMessageRequest, registry: RegistryDep, settings: SettingsDep
) -> LegacyResult:
    """Display a message on the sign, as the old API did.

    The message goes into a reserved slot rather than the sign's priority file,
    so other sources can share the sign with it.
    """
    try:
        await registry.upsert(
            LEGACY_SLOT_KEY,
            body.message,
            mode=body.display_mode.strip().upper(),
            position="MIDDLE",
            source="compat",
            # Unset by default, which is exactly how this endpoint has always
            # behaved: the message stays until something replaces it. Set it and
            # an automation that stops calling leaves an empty sign rather than
            # a stale temperature that still looks current.
            ttl_seconds=settings.legacy_slot_ttl_seconds,
            # Unknown tokens are passed through as literal text, exactly as the
            # old parser did, so a payload that worked before cannot start
            # failing now.
            strict=False,
        )
    except KeyError:
        return LegacyResult.error(
            "The display mode '%s' is not valid" % body.display_mode
        )
    except Exception as err:
        logger.warning("compat write failed: %s", err)
        return LegacyResult.error(str(err))

    return LegacyResult.ok("Message displayed on sign")


@write.post(
    "/ControlCommand",
    summary="Send a control command to the sign",
    dependencies=[RequireApiKey],
)
async def write_control_command(
    body: LegacyCommandRequest, controller: ControllerDep
) -> LegacyResult:
    """Send one of the sign's control commands, as the old API did."""
    try:
        payload = commands.build(body.command, body.parameter)
    except commands.UnknownCommand:
        # The old wording, kept because something may be matching on it.
        return LegacyResult.error("Unrecognized control command '%s'" % body.command)
    except commands.BadParameter as err:
        return LegacyResult.error(str(err))

    try:
        await controller.send_special(payload)
    except Exception as err:
        logger.warning("compat control command failed: %s", err)
        return LegacyResult.error(str(err))

    return LegacyResult.ok("Control command sent to sign")


@enumerations.get("/DisplayModes", summary="Available display modes")
async def display_modes() -> list[LegacyDisplayMode]:
    """List the display modes, in the old shape."""
    return [
        LegacyDisplayMode(display_mode=token.text, description=token.description)
        for token in DISPLAY_MODES
    ]


@enumerations.get("/ControlCommands", summary="Available control commands")
async def control_commands() -> list[LegacyControlCommand]:
    """List the control commands, in the old shape."""
    return [
        LegacyControlCommand(control_command=token.text, description=token.description)
        for token in CONTROL_COMMANDS
    ]


@enumerations.get("/MarkupTokens", summary="Available markup tokens")
async def markup_tokens() -> list[LegacyToken]:
    """List the markup tokens, in the old shape.

    The old implementation had the two field descriptions the wrong way round,
    describing ``token_text`` as the description and ``description`` as the
    token text. The field names and values were always right, so only the
    documentation changes here.
    """
    return [
        LegacyToken(token_text=token.text, description=token.description)
        for token in MARKUP_TOKENS
    ]


router.include_router(write)
router.include_router(enumerations)
