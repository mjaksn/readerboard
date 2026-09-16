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

from readerboard.protocol.constants import PICTURE_FILE_LABELS

DEFAULT_CONFIG_FILE = Path("/etc/readerboard/config.toml")

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
    picture_count: int = Field(
        default=0,
        ge=0,
        le=len(PICTURE_FILE_LABELS),
        description=(
            "how many of the built-in icons can be on the sign at once, each a picture "
            "file of its own that a message draws with <icon:name>. There are 148 icons "
            "and this is how many fit, not how many exist: a file is claimed by whichever "
            "icon a message calls and kept until another needs it. 0 turns icons off, and "
            "is the default because raising it reallocates the sign and clears it"
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
            "how often to check that the sign still holds what it was given, and to "
            "push all of it again if it does not. This is what repairs a sign that was "
            "power cycled behind a still-connected Ethernet adapter, which nothing else "
            "can detect. The check is one short read, so an interval that costs nothing "
            "on a sign that is fine is the point of it"
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
        sign, and the sign is asked before it is reconfigured instead, where the
        link is open.

        This one is a ceiling and the sign's own answer can only lower it. A
        configuration bigger than the assumption never reaches the sign to be
        asked about, so a sign with a larger pool cannot permit one. See
        readerboard.sign.pool for why it is that way round.
        """
        from readerboard.protocol.frames import memory_claimed_by
        from readerboard.sign.layout import PICTURE_COLUMNS, PICTURE_ROWS
        from readerboard.sign.pool import ASSUMED_SIGN_MEMORY_POOL

        claimed = memory_claimed_by(
            [self.slot_capacity] * self.slot_count
            + [self.variable_capacity] * self.variable_count
            # A picture's allocated size is a geometry rather than a byte count,
            # so what it takes out of the pool is its pixels rather than that
            # field. The sign packs two to a byte, measured on 2026-09-12.
            + [PICTURE_ROWS * PICTURE_COLUMNS // 2] * self.picture_count
        )
        if claimed > ASSUMED_SIGN_MEMORY_POOL:
            raise ValueError(
                "slot_count %d at slot_capacity %d, variable_count %d at "
                "variable_capacity %d and picture_count %d need %d bytes of the sign's "
                "memory pool, and a BetaBrite Classic has %d. Lower one of them. This "
                "is the most any sign driven from here may be configured with, whatever "
                "a particular sign reports."
                % (
                    self.slot_count,
                    self.slot_capacity,
                    self.variable_count,
                    self.variable_capacity,
                    self.picture_count,
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
