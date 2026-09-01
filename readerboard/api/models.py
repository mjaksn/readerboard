"""Request and response shapes.

The ``Simple*`` models are the shapes the ``/Write`` and ``/Enumerations``
endpoints use, where the outcome is carried in the body as well as in the status
code. The rest belong to ``/v2``, which reports through the status code and
FastAPI's ``detail`` body.

Every ``description`` here ends up in the OpenAPI page, so it is documentation
in the same sense the README is, and it rots the same way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from readerboard.protocol.tokens import COMMAND_BY_NAME, MODE_BY_NAME, POSITION_BY_NAME
from readerboard.sign.state import AlertState, SlotState

SlotKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="the name of the slot, chosen by whoever owns it",
    ),
]


def _normalise_mode(value: str) -> str:
    upper = value.strip().upper()
    if upper not in MODE_BY_NAME:
        raise ValueError(
            "unknown display mode %r; see GET /v2/enumerations/display-modes" % value
        )
    return upper


def _normalise_position(value: str) -> str:
    upper = value.strip().upper()
    if upper not in POSITION_BY_NAME:
        raise ValueError(
            "unknown text position %r; see GET /v2/enumerations/text-positions" % value
        )
    return upper


class MessageRequest(BaseModel):
    """A message registered into a slot."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        max_length=4096,
        description="the message, including markup tokens such as <red> and <degree>",
    )
    display_mode: str = Field(default="HOLD", description="how the sign presents the message")
    position: str = Field(default="MIDDLE", description="where the text sits vertically")
    order: int = Field(
        default=0,
        description="lower numbers play earlier in the rotation; ties break on the slot name",
    )
    ttl_seconds: float | None = Field(
        default=None,
        gt=0,
        description="drop the message this many seconds from now; omit to keep it until replaced",
    )
    source: str | None = Field(
        default=None,
        max_length=128,
        description="who registered this, recorded so the slot list is readable",
    )

    _check_mode = field_validator("display_mode")(_normalise_mode)
    _check_position = field_validator("position")(_normalise_position)


class SlotResponse(BaseModel):
    """A registered slot."""

    key: str
    label: str = Field(description="the sign file this slot occupies, A through Z")
    message: str
    display_mode: str
    position: str
    order: int
    source: str | None
    expires_at: datetime | None
    updated_at: datetime

    @classmethod
    def of(cls, slot: SlotState) -> SlotResponse:
        """Render a stored slot as the API's view of it."""
        return cls(
            key=slot.key,
            label=slot.label,
            message=slot.message,
            display_mode=slot.mode,
            position=slot.position,
            order=slot.order,
            source=slot.source,
            expires_at=slot.expires_at,
            updated_at=slot.updated_at,
        )


class AlertRequest(BaseModel):
    """A message that takes the whole sign over until it is released."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        max_length=4096,
        description=(
            "the alert text. The sign's priority file holds 125 bytes once markup has "
            "been rendered, and cannot be resized"
        ),
    )
    display_mode: str = Field(default="HOLD", description="how the sign presents the alert")
    position: str = Field(default="MIDDLE", description="where the text sits vertically")
    ttl_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "release the sign this many seconds from now. Omit and the alert holds the "
            "sign until something releases it explicitly"
        ),
    )

    _check_mode = field_validator("display_mode")(_normalise_mode)
    _check_position = field_validator("position")(_normalise_position)


class AlertResponse(BaseModel):
    """The alert currently holding the sign."""

    message: str
    display_mode: str
    position: str
    started_at: datetime
    expires_at: datetime | None

    @classmethod
    def of(cls, alert: AlertState) -> AlertResponse:
        """Render a stored alert as the API's view of it."""
        return cls(
            message=alert.message,
            display_mode=alert.mode,
            position=alert.position,
            started_at=alert.started_at,
            expires_at=alert.expires_at,
        )


class ControlCommandRequest(BaseModel):
    """A command aimed at the sign itself rather than at a message."""

    model_config = ConfigDict(extra="forbid")

    command: str = Field(description="one of %s" % ", ".join(sorted(COMMAND_BY_NAME)))
    parameter: str = Field(default="", description="the command's parameter, if it takes one")


class ClockResponse(BaseModel):
    """The result of setting the sign's clock."""

    synced_at: datetime = Field(description="the time the sign was told, in its configured zone")


class LinkHealth(BaseModel):
    """The state of the link to the sign."""

    url: str = Field(description="the configured pyserial URL")
    connected: bool
    last_write_at: datetime | None
    last_error: str | None
    writes: int
    suppressed_writes: int = Field(
        description="writes skipped because the sign already held those exact bytes"
    )


class HealthResponse(BaseModel):
    """What the service knows about itself. Requires no API key."""

    status: str = Field(description="'ok' when the sign is reachable, otherwise 'degraded'")
    version: str
    link: LinkHealth
    slots_used: int
    slots_total: int
    sign_in_sync: bool = Field(
        description=(
            "false when a registered message has been accepted but not yet written to "
            "the sign, which is what a write during an outage looks like"
        )
    )
    alert_active: bool
    clock_last_synced_at: datetime | None


class TokenInfo(BaseModel):
    """One entry in an enumeration."""

    name: str
    description: str


# ===========================================================================
# The simple API's shapes. Every response carries the outcome in the body, and
# a status code that says the same thing.
# ===========================================================================


class SimpleMessageRequest(BaseModel):
    """The body POST /Write/Message has always accepted."""

    display_mode: str = Field(description="the display mode to use when showing the message")
    message: str = Field(
        description="the message, including markup tokens, to display on the sign"
    )


class SimpleCommandRequest(BaseModel):
    """The body POST /Write/ControlCommand has always accepted."""

    command: str = Field(description="the control command to send to the sign")
    parameter: str = Field(default="", description="a parameter for the command")


class SimpleResult(BaseModel):
    """The body the simple endpoints return, whatever happened.

    The status code beside it says the same thing. This shape has not changed
    with it, so anything reading ``result`` and ``result_message`` reads exactly
    what it always did.
    """

    result: str = Field(description="OK or ERROR")
    result_message: str = Field(description="text description of the command result")

    @classmethod
    def ok(cls, message: str) -> SimpleResult:
        """Build a success, in the old shape."""
        return cls(result="OK", result_message=message)

    @classmethod
    def error(cls, message: str) -> SimpleResult:
        """Build a failure, in the old shape.

        The status code is the caller's to set, because only the caller knows
        which failure this is. :func:`readerboard.api.errors.status_for` is
        what turns one of the service's own exceptions into that answer.
        """
        return cls(result="ERROR", result_message=message)


class SimpleDisplayMode(BaseModel):
    """One entry of GET /Enumerations/DisplayModes."""

    display_mode: str
    description: str


class SimpleControlCommand(BaseModel):
    """One entry of GET /Enumerations/ControlCommands."""

    control_command: str
    description: str


class SimpleToken(BaseModel):
    """One entry of GET /Enumerations/MarkupTokens."""

    token_text: str
    description: str
