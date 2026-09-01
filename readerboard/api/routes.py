"""The API.

Status codes mean what they say here: 400 for a message the sign cannot render,
401 for a missing key, 404 for a slot that does not exist, 409 when the pool is
full, and 503 when the sign is unreachable. Which exception means which lives in
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
    SlotKey,
    SlotResponse,
    TokenInfo,
)
from readerboard.protocol.tokens import (
    CONTROL_COMMANDS,
    DISPLAY_MODES,
    MARKUP_TOKENS,
    TEXT_POSITIONS,
    Token,
)
from readerboard.services import commands

router = APIRouter()

messages = APIRouter(prefix="/messages", tags=["Messages"])
alerts_routes = APIRouter(prefix="/alerts", tags=["Alerts"])
sign_routes = APIRouter(prefix="/sign", tags=["Sign"])
enumerations = APIRouter(prefix="/enumerations", tags=["Enumerations"])


# ===========================================================================
# Messages
# ===========================================================================


@messages.get("", summary="List the messages sharing the sign")
async def list_messages(registry: RegistryDep) -> list[SlotResponse]:
    """Return every registered slot, in the order the sign plays them."""
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

    The sign rotates through every registered slot on its own, so registering a
    second message does not displace the first.
    """
    slot = await registry.upsert(
        key,
        body.message,
        mode=body.display_mode,
        position=body.position,
        order=body.order,
        ttl_seconds=body.ttl_seconds,
        source=body.source,
    )
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
    """
    alert = await alerts.raise_alert(
        body.message,
        mode=body.display_mode,
        position=body.position,
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
    """
    return ClockResponse(synced_at=await clock.sync())


@sign_routes.post(
    "/command",
    summary="Send a control command to the sign",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[RequireApiKey],
)
async def send_command(body: ControlCommandRequest, controller: ControllerDep) -> Response:
    """Send one of the sign's own control commands."""
    await controller.send_special(commands.build(body.command, body.parameter))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ===========================================================================
# Enumerations
# ===========================================================================


def _as_info(tokens: tuple[Token, ...]) -> list[TokenInfo]:
    return [TokenInfo(name=token.text, description=token.description) for token in tokens]


@enumerations.get("/markup-tokens", summary="Markup tokens a message may contain")
async def markup_tokens() -> list[TokenInfo]:
    """List every token that can be written inline in a message."""
    return _as_info(MARKUP_TOKENS)


@enumerations.get("/display-modes", summary="Ways the sign can present a message")
async def display_modes() -> list[TokenInfo]:
    """List every display mode."""
    return _as_info(DISPLAY_MODES)


@enumerations.get("/text-positions", summary="Where text sits vertically")
async def text_positions() -> list[TokenInfo]:
    """List every vertical text position."""
    return _as_info(TEXT_POSITIONS)


@enumerations.get("/control-commands", summary="Commands aimed at the sign itself")
async def control_commands() -> list[TokenInfo]:
    """List every control command."""
    return _as_info(CONTROL_COMMANDS)


router.include_router(messages)
router.include_router(alerts_routes)
router.include_router(sign_routes)
router.include_router(enumerations)
