"""Getting at the running service's parts from inside a request, and the API key.

Everything the service owns is built once during startup and hung on
``app.state``. These accessors are what routes depend on, so a route never
reaches into application state directly and tests can build the pieces
themselves.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status

from readerboard.config import Settings
from readerboard.services.alerts import AlertService
from readerboard.services.clock import ClockService
from readerboard.services.registry import MessageRegistry
from readerboard.sign.controller import SignController

API_KEY_HEADER = "X-API-Key"


def get_settings(request: Request) -> Settings:
    """Return the service's configuration."""
    settings: Settings = request.app.state.settings
    return settings


def get_controller(request: Request) -> SignController:
    """Return the single writer that owns the sign."""
    controller: SignController = request.app.state.controller
    return controller


def get_registry(request: Request) -> MessageRegistry:
    """Return the registered messages."""
    registry: MessageRegistry = request.app.state.registry
    return registry


def get_alerts(request: Request) -> AlertService:
    """Return the alert service."""
    alerts: AlertService = request.app.state.alerts
    return alerts


def get_clock(request: Request) -> ClockService:
    """Return the clock sync."""
    clock: ClockService = request.app.state.clock
    return clock


def require_api_key(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias=API_KEY_HEADER)] = None,
) -> None:
    """Reject a write that does not carry the configured API key.

    Compared with :func:`hmac.compare_digest` so that a wrong key cannot be
    narrowed down by timing. The key itself is never logged or echoed, here or
    anywhere else.

    This is the one place the compatibility endpoints are allowed to break their
    "always 200" rule. A caller without the key is not a caller whose request
    failed; it is a caller the service will not talk to.
    """
    expected: str = request.app.state.settings.api_key.get_secret_value()

    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "no API key is configured, so every write is refused. Set api_key in "
                "the config file or READERBOARD_API_KEY in the environment."
            ),
        )

    if x_api_key is None or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="a valid %s header is required" % API_KEY_HEADER,
            headers={"WWW-Authenticate": API_KEY_HEADER},
        )


SettingsDep = Annotated[Settings, Depends(get_settings)]
ControllerDep = Annotated[SignController, Depends(get_controller)]
RegistryDep = Annotated[MessageRegistry, Depends(get_registry)]
AlertsDep = Annotated[AlertService, Depends(get_alerts)]
ClockDep = Annotated[ClockService, Depends(get_clock)]
RequireApiKey = Depends(require_api_key)
