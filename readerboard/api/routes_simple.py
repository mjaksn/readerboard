"""A smaller surface for callers that post a fixed body to a fixed path.

Fixed paths, one message, no slot to name, and a ``result`` of ``OK`` or
``ERROR`` in the body. That suits a Home Assistant ``rest_command`` or a shell
one-liner in a cron job, neither of which wants to name a slot or read a slot
table back.

The status code says the same thing the body does. It did not always: every
response here used to be a 200 whatever happened, on the theory that those two
callers do not branch on status codes. They do not, but neither do they read a
JSON body, so the outcome only ever lived in the one place they were least
likely to look. A ``rest_command`` fired without ``response_variable`` never saw
``result_message`` at all, and plain ``curl`` in a cron job exits 0 and prints it
to a log nobody reads. A status code is the one thing both of them surface on
their own, so a failed write is now visible to a caller that does nothing to
look for it, which it was not before.

The body shape is untouched, so anything that does read ``result`` and
``result_message`` sees exactly what it saw before.

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
from typing import Any

from fastapi import APIRouter, Response, status

from readerboard.api import errors
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

# What the two writes can answer with when they fail, declared so the page shows
# what is true of all of them: the body is the same shape whatever the status,
# and a caller that only reads ``result`` need not care which code it got. Which
# code any given failure earns is decided in readerboard.api.errors, not here.
# A 401 and a 422 are not listed because neither reaches these routes.
SIMPLE_FAILURES: dict[int | str, dict[str, Any]] = {
    status.HTTP_400_BAD_REQUEST: {
        "model": SimpleResult,
        "description": "The sign cannot render it, or the mode or command is not one it has",
    },
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "model": SimpleResult,
        "description": "The sign is unreachable",
    },
    status.HTTP_500_INTERNAL_SERVER_ERROR: {
        "model": SimpleResult,
        "description": "The service raised something it does not have a code for",
    },
}


@write.post(
    "/Message",
    summary="Write a message to the sign",
    dependencies=[RequireApiKey],
    responses=SIMPLE_FAILURES,
)
async def write_message(
    body: SimpleMessageRequest,
    registry: RegistryDep,
    settings: SettingsDep,
    response: Response,
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
        # Not one of the service's own exceptions, so the shared table has
        # nothing to say about it. A mode this sign does not have is the
        # caller's mistake either way.
        response.status_code = status.HTTP_400_BAD_REQUEST
        return SimpleResult.error(
            "The display mode '%s' is not valid" % body.display_mode
        )
    except Exception as err:
        logger.warning("simple write failed: %s", err)
        response.status_code = errors.status_for(err)
        return SimpleResult.error(str(err))

    return SimpleResult.ok("Message displayed on sign")


@write.post(
    "/ControlCommand",
    summary="Send a control command to the sign",
    dependencies=[RequireApiKey],
    responses=SIMPLE_FAILURES,
)
async def write_control_command(
    body: SimpleCommandRequest, controller: ControllerDep, response: Response
) -> SimpleResult:
    """Send one of the sign's control commands."""
    try:
        payload = commands.build(body.command, body.parameter)
    except commands.UnknownCommand as err:
        # The old wording, kept because something may be matching on it.
        response.status_code = errors.status_for(err)
        return SimpleResult.error("Unrecognized control command '%s'" % body.command)
    except commands.BadParameter as err:
        response.status_code = errors.status_for(err)
        return SimpleResult.error(str(err))

    try:
        await controller.send_special(payload)
    except Exception as err:
        logger.warning("simple control command failed: %s", err)
        response.status_code = errors.status_for(err)
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
