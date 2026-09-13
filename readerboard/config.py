"""Everything the service reads from its environment, in one place.

Settings come from three places, later ones winning: the defaults below, a TOML
file (``/etc/readerboard/config.toml`` unless ``READERBOARD_CONFIG_FILE`` says
otherwise), and environment variables prefixed ``READERBOARD_``.

One change here is worth calling out because it will bite on upgrade. The old
``config.json`` held ``com_port``, a bare device name that the server prefixed
with ``/dev/`` before opening. This service takes a full pyserial URL in
``serial_url`` instead, so a sign on an Ethernet adapter is
``socket://192.168.2.51:4001`` and a sign on a cable is ``/dev/ttyUSB0``. There
is no prefixing, and no way to express a network sign in the old key.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

DEFAULT_CONFIG_FILE = Path("/etc/readerboard/config.toml")

# What the service assumes the sign's memory pool is when it cannot ask.
#
# A BetaBrite Classic reported 5482 bytes on 2026-09-12, read back from the sign
# itself. An earlier figure here was 26000, taken from a remembered claim that
# the sign holds around 30000 bytes of messages and graphics; it is nearly five
# times the pool this hardware actually has, so the check it backed could not do
# its job: a configuration with no room to exist passed it and went to the sign,
# where what happens to one has never been measured.
#
# This is the first tier of the check rather than the whole of it. The service asks
# the sign for its own figure at startup and uses that; see readerboard.sign.pool
# for why the check is in two tiers and why this one cannot ask.
ASSUMED_SIGN_MEMORY_POOL = 5482


def _config_file() -> Path:
    override = os.environ.get("READERBOARD_CONFIG_FILE")
    return Path(override) if override else DEFAULT_CONFIG_FILE


class Settings(BaseSettings):
    """The service's configuration."""

    model_config = SettingsConfigDict(
        env_prefix="READERBOARD_",
        toml_file=_config_file(),
        extra="forbid",
    )

    # == the link to the sign ==============================================

    serial_url: str = Field(
        default="loop://",
        description=(
            "pyserial URL for the sign: socket://host:port for an Ethernet to RS-232 "
            "adapter, /dev/ttyUSB0 for a direct cable, or loop:// to run without one"
        ),
    )
    baud_rate: int = Field(default=9600, ge=110, le=921600)
    serial_timeout: float = Field(default=10.0, gt=0)
    inter_packet_delay: float = Field(
        default=0.25,
        ge=0,
        le=10,
        description=(
            "seconds to wait after each transmission before sending another. The "
            "default is what a BetaBrite Classic was measured taking six writes in a "
            "row at, with the same run failing at 0.1; run scripts/protocol_spike.py "
            "to find what your own sign needs"
        ),
    )
    settle_delays_enabled: bool = Field(
        default=True,
        description=(
            "whether to wait out the windows in which the sign cannot listen, after a "
            "reset or a tone. Turn this off only when the far end is not a sign: the "
            "simulator has no diagnostics to run and no speaker to switch its port off "
            "for. Against a real sign it costs the writes that land while it is deaf, "
            "which fail silently"
        ),
    )
    backoff_initial: float = Field(default=1.0, gt=0)
    backoff_max: float = Field(default=60.0, gt=0)

    # == the pool of sign files the rotation uses ==========================

    slot_count: int = Field(
        default=8,
        ge=1,
        le=26,
        description="how many messages can share the sign at once, one sign file each",
    )
    slot_capacity: int = Field(
        default=256,
        ge=16,
        le=4096,
        description=(
            "bytes allocated to each message, after markup has been rendered. This and "
            "the three settings around it come out of the sign's memory pool, which is "
            "5482 bytes on a BetaBrite Classic, and each file costs thirteen bytes of "
            "overhead beyond its own size"
        ),
    )
    variable_count: int = Field(
        default=8,
        ge=0,
        le=26,
        description=(
            "how many variables the sign can hold, each a small file of its own that a "
            "message calls with <var:name>. 0 turns variables off"
        ),
    )
    variable_capacity: int = Field(
        default=32,
        ge=1,
        le=125,
        description=(
            "bytes allocated to each variable's value, after markup has been rendered. "
            "The sign allows no more than 125"
        ),
    )

    # == behaviour =========================================================

    registry_sweep_seconds: float = Field(
        default=1.0,
        gt=0,
        description=(
            "how often to look for messages, alerts and variables whose ttl has passed. "
            "An expiry lands up to this much after its deadline, so it is kept short: a "
            "sweep that finds nothing due writes nothing to the sign"
        ),
    )
    refresh_interval_seconds: float = Field(
        default=900.0,
        gt=0,
        description=(
            "how often to push every message to the sign again whether or not it looks "
            "necessary. This is what repairs a sign that was power cycled behind a "
            "still-connected Ethernet adapter, which nothing else can detect"
        ),
    )
    clock_sync_enabled: bool = True
    clock_sync_interval_seconds: float = Field(default=3600.0, gt=0)
    timezone: str | None = Field(
        default=None,
        description=(
            "IANA name such as America/New_York, used when setting the sign's clock. "
            "Unset means the machine's own local time"
        ),
    )

    # == the HTTP surface ==================================================

    host: str = "0.0.0.0"
    port: int = Field(default=5001, ge=1, le=65535)
    api_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "required on every write. Generated by scripts/install.sh when it"
            " first writes the config file"
        ),
    )
    open_docs: bool = Field(
        default=False,
        description=(
            "open the documentation page in a browser once the service is "
            "listening. For running from an editor; an installed service has "
            "nobody at the machine to show it to, which is why it is off unless "
            "asked for"
        ),
    )

    # == where state and logs go ===========================================

    state_path: Path = Path("/var/lib/readerboard/state.json")
    log_level: str = "INFO"
    log_file: Path | None = None

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError("log_level must be one of %s" % ", ".join(sorted(allowed)))
        return upper

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise ValueError("unknown timezone %r: %s" % (value, err)) from err
        return value

    @model_validator(mode="after")
    def _check_pool_fits(self) -> Settings:
        """Refuse a pool no BetaBrite Classic could hold.

        This is the tier of the check that runs with no sign present, because
        it runs wherever settings are read: in the tests, in
        scripts/dump_openapi.py, and on a machine whose sign is unplugged. So it
        measures against ``ASSUMED_SIGN_MEMORY_POOL`` rather than against the
        sign, and the sign is asked at startup instead, where the link is open
        and its own answer can win. See readerboard.sign.pool.
        """
        from readerboard.protocol.frames import memory_claimed_by

        claimed = memory_claimed_by(
            [self.slot_capacity] * self.slot_count
            + [self.variable_capacity] * self.variable_count
        )
        if claimed > ASSUMED_SIGN_MEMORY_POOL:
            raise ValueError(
                "slot_count %d at slot_capacity %d and variable_count %d at "
                "variable_capacity %d need %d bytes of the sign's memory pool, and a "
                "BetaBrite Classic has %d. Lower one of them."
                % (
                    self.slot_count,
                    self.slot_capacity,
                    self.variable_count,
                    self.variable_capacity,
                    claimed,
                    ASSUMED_SIGN_MEMORY_POOL,
                )
            )
        if self.backoff_max < self.backoff_initial:
            raise ValueError("backoff_max must not be smaller than backoff_initial")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Put the TOML file below the environment but above the defaults."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    def redacted(self) -> dict[str, Any]:
        """Return the settings as a dict with the API key removed, safe to log."""
        data = self.model_dump(mode="json")
        data["api_key"] = "set" if self.api_key.get_secret_value() else "unset"
        return data
