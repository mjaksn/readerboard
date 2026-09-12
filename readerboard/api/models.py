"""Request and response shapes.

A failure is reported through the status code and FastAPI's ``detail`` body
rather than in a field of its own, so none of these shapes has an outcome field.

Every ``description`` here ends up in the OpenAPI page, so it is documentation
in the same sense the README is, and it rots the same way.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from readerboard.protocol.markup import VARIABLE_NAME_PATTERN
from readerboard.protocol.replies import GeneralInformation
from readerboard.protocol.tokens import COMMAND_BY_NAME, MODE_BY_NAME
from readerboard.sign.state import AlertState, SlotState, VariableState

SlotKey = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]+$",
        description="the name of the slot, chosen by whoever owns it",
    ),
]

# The same pattern the markup checks a <var:name> against, so that no variable
# can be created under a name no message could call.
VariableName = Annotated[
    str,
    Field(
        pattern=VARIABLE_NAME_PATTERN,
        description=(
            "the variable's name, one to 32 lowercase letters, digits and underscores. "
            "A message calls it as <var:name>"
        ),
    ),
]


ON_EXPIRY_CHOICES = ("delete", "deactivate")


def _normalise_on_expiry(value: str) -> str:
    lower = value.strip().lower()
    if lower not in ON_EXPIRY_CHOICES:
        raise ValueError(
            "on_expiry must be one of %s, got %r" % (", ".join(ON_EXPIRY_CHOICES), value)
        )
    return lower


def _normalise_mode(value: str) -> str:
    upper = value.strip().upper()
    if upper not in MODE_BY_NAME:
        raise ValueError(
            "unknown display mode %r; see GET /enumerations/display-modes" % value
        )
    return upper


class MessageRequest(BaseModel):
    """A message registered into a slot."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "the message, including markup tokens such as <red> and <degree>, and "
            "<var:name> to call a variable, which has to exist first. It cannot be "
            "empty: an empty message holds a slot open around nothing, and the sign "
            "cycles to a file with no text in it. Use DELETE to give the slot back"
        ),
    )
    display_mode: str = Field(default="HOLD", description="how the sign presents the message")
    order: int = Field(
        default=0,
        description="lower numbers play earlier in the rotation; ties break on the slot name",
    )
    ttl_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "act on the message this many seconds from now, deleting or hiding it "
            "according to on_expiry; omit to keep it showing until it is replaced"
        ),
    )
    on_expiry: str = Field(
        default="delete",
        description=(
            "what happens when ttl_seconds passes: delete gives the slot back, and "
            "deactivate keeps it registered but takes it off the display, so it can be "
            "shown again without being sent afresh. Nothing without a ttl_seconds"
        ),
    )
    source: str | None = Field(
        default=None,
        max_length=128,
        description="who registered this, recorded so the slot list is readable",
    )

    _check_mode = field_validator("display_mode")(_normalise_mode)
    _check_on_expiry = field_validator("on_expiry")(_normalise_on_expiry)


class SlotActiveRequest(BaseModel):
    """Whether the sign should be playing a slot."""

    model_config = ConfigDict(extra="forbid")

    active: bool = Field(
        description=(
            "true to put the message into the rotation, false to take it off the display "
            "while it stays registered, keeping its slot, its file, its text and its place "
            "in the order"
        )
    )


class SlotResponse(BaseModel):
    """A registered slot."""

    key: str
    label: str = Field(description="the sign file this slot occupies, A through Z")
    message: str
    display_mode: str
    order: int
    active: bool = Field(
        description="whether the sign is playing it; a false one stays registered and hidden"
    )
    on_expiry: str = Field(description="what ttl_seconds does when it passes")
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
            order=slot.order,
            active=slot.active,
            on_expiry=slot.on_expiry,
            source=slot.source,
            expires_at=slot.expires_at,
            updated_at=slot.updated_at,
        )


class VariableRequest(BaseModel):
    """A value for a variable, which the messages calling it show."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(
        max_length=1024,
        description=(
            "the value, with the same markup a message takes except <week_day> and "
            "<var:name>, which the sign draws as a literal character from inside a "
            "variable. Formatting set here carries on into the message after the call: "
            "a value of <red>DOWN turns the rest of the message red too. May be empty, "
            "which shows nothing where the variable is called"
        ),
    )
    ttl_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "show stale_value in its place this many seconds from now unless a fresh "
            "value arrives first. The variable itself stays, since messages call it. "
            "Omit to keep the value until replaced"
        ),
    )
    stale_value: str = Field(
        default="",
        max_length=1024,
        description=(
            "what to show once ttl_seconds has passed, such as --. Empty shows nothing. "
            "Checked against the variable's size now, not when it is needed"
        ),
    )
    source: str | None = Field(
        default=None,
        max_length=128,
        description="who wrote this, recorded so the variable list is readable",
    )


class VariableResponse(BaseModel):
    """A variable."""

    name: str
    label: str = Field(description="the sign file this variable occupies, a through z")
    value: str
    stale_value: str
    stale: bool = Field(
        description="true once ttl_seconds has passed, when the sign shows stale_value instead"
    )
    source: str | None
    expires_at: datetime | None = Field(
        description=(
            "when the value goes stale, or null if it never will or already has, which "
            "stale tells apart"
        )
    )
    updated_at: datetime
    called_by: list[str] = Field(
        description=(
            "the keys of the slots whose messages call this variable. It cannot be deleted "
            "while any do, nor while called_by_alert is true"
        )
    )
    called_by_alert: bool = Field(
        description=(
            "true while the alert holding the sign calls this variable. It cannot be "
            "deleted until the alert is released or replaced with one that does not"
        )
    )

    @classmethod
    def of(
        cls, variable: VariableState, called_by: list[str], *, called_by_alert: bool
    ) -> VariableResponse:
        """Render a stored variable as the API's view of it."""
        return cls(
            name=variable.name,
            label=variable.label,
            value=variable.value,
            stale_value=variable.stale_value,
            stale=variable.stale,
            source=variable.source,
            expires_at=variable.expires_at,
            updated_at=variable.updated_at,
            called_by=called_by,
            called_by_alert=called_by_alert,
        )


class SignInformationResponse(BaseModel):
    """What the sign says about itself."""

    firmware_version: str = Field(description="the firmware build the sign is running")
    firmware_revision: str = Field(
        description="its revision letter, empty on a sign that does not report one"
    )
    firmware_released: str = Field(description="the month and year of that firmware, as MM/YY")
    clock: str = Field(description="the sign's own clock, as HH:MM on a 24 hour dial")
    time_format: str = Field(description="how the sign draws <time>, '12 hour' or '24 hour'")
    speaker_enabled: bool = Field(
        description="whether the speaker will make a noise when SOUND is sent"
    )
    memory_total: int = Field(description="the size of the sign's memory pool, in bytes")
    memory_free: int = Field(description="how much of that pool is unused, in bytes")
    raw: str = Field(description="the sign's answer as it arrived, for when the fields are not enough")

    @classmethod
    def of(cls, info: GeneralInformation) -> SignInformationResponse:
        """Render a parsed reply as the API's view of it."""
        return cls(
            firmware_version=info.firmware_version,
            firmware_revision=info.firmware_revision,
            firmware_released=info.firmware_released,
            clock=info.clock,
            time_format=info.time_format,
            speaker_enabled=info.speaker_enabled,
            memory_total=info.memory_total,
            memory_free=info.memory_free,
            raw=info.raw,
        )


class AlertRequest(BaseModel):
    """A message that takes the whole sign over until it is released."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "the alert text. The sign's priority file holds 125 bytes once markup has "
            "been rendered, and cannot be resized. It can call variables with "
            "<var:name>, as a message can. It cannot be empty: a write with no "
            "text still carries the formatting bytes around it, which the sign reads as "
            "a blank priority message and displays, so the sign would sit blank with "
            "the rotation suppressed behind it and an alert reported as active"
        ),
    )
    display_mode: str = Field(default="HOLD", description="how the sign presents the alert")
    ttl_seconds: float | None = Field(
        default=None,
        gt=0,
        description=(
            "release the sign this many seconds from now. Omit and the alert holds the "
            "sign until something releases it explicitly"
        ),
    )

    _check_mode = field_validator("display_mode")(_normalise_mode)


class AlertResponse(BaseModel):
    """The alert currently holding the sign."""

    message: str
    display_mode: str
    started_at: datetime
    expires_at: datetime | None

    @classmethod
    def of(cls, alert: AlertState) -> AlertResponse:
        """Render a stored alert as the API's view of it."""
        return cls(
            message=alert.message,
            display_mode=alert.mode,
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

    synced_at: datetime = Field(
        description=(
            "when the clock was set, in its configured zone. Not what the sign was told: "
            "the sign is deliberately set one minute ahead, because the protocol's Set "
            "Time carries no seconds and a sign told the current minute runs behind for "
            "the rest of it"
        )
    )


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
    variables_used: int
    variables_total: int = Field(description="0 when variables are switched off")
    sign_in_sync: bool = Field(
        description=(
            "false when the sign is behind the service's record, which is a removal or "
            "an expiry that could not reach the sign yet; a message write that cannot "
            "reach the sign is refused with a 503 rather than held"
        )
    )
    alert_active: bool
    clock_last_synced_at: datetime | None


class TokenInfo(BaseModel):
    """One entry in an enumeration."""

    name: str
    description: str
