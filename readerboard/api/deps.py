"""Getting at the running service's parts from inside a request, and the API key.

Everything the service owns is built once during startup and hung on
``app.state``. These accessors are what routes depend on, so a route never
reaches into application state directly and tests can build the pieces
themselves.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from readerboard.services.alerts import AlertService
from readerboard.services.clock import ClockService
from readerboard.services.registry import MessageRegistry
from readerboard.sign.controller import SignController

API_KEY_HEADER = "X-API-Key"

# Declaring the key as a security scheme rather than as a plain header parameter
# is what puts the Authorize button in the Swagger UI, and what makes a generated
# client treat it as a credential rather than as one more header to fill in per
# call. The header and the value are exactly what they always were.
#
# `auto_error=False` is load bearing. Left at its default the scheme rejects a
# missing key itself, with its own wording and with no way to tell "you sent no
# key" apart from "this service has no key configured at all". Those are
# different answers, 401 and 503, and both are documented, so the checking stays
# in `require_api_key` below and this declares the scheme and nothing else.
# The scheme name becomes a key under `components.securitySchemes`, and the
# OpenAPI specification requires those to match `^[a-zA-Z0-9\.\-_]+$` (section
# 4.8.7.1). So it cannot be the prettier "API key", however much better that
# reads in the Authorize dialog: a space there makes the whole document invalid
# and trips validators and client generators. The human wording lives in the
# description below, which is what the dialog shows underneath the name.
api_key_scheme = APIKeyHeader(
    name=API_KEY_HEADER,
    auto_error=False,
    scheme_name="ApiKeyAuth",
    description=(
        "The shared key every write carries. Set it with `api_key` in the config "
        "file or `READERBOARD_API_KEY` in the environment; `scripts/install.sh` "
        "generates one."
    ),
)


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
    x_api_key: Annotated[str | None, Security(api_key_scheme)] = None,
) -> None:
    """Reject a write that does not carry the configured API key.

    Compared with :func:`hmac.compare_digest` so that a wrong key cannot be
    narrowed down by timing. The key itself is never logged or echoed, here or
    anywhere else.

    A 401 rather than anything a route decides. A caller without the key is not
    a caller whose request failed; it is a caller the service will not talk to,
    and it never reaches a route to be answered by one.
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


ControllerDep = Annotated[SignController, Depends(get_controller)]
RegistryDep = Annotated[MessageRegistry, Depends(get_registry)]
AlertsDep = Annotated[AlertService, Depends(get_alerts)]
ClockDep = Annotated[ClockService, Depends(get_clock)]
RequireApiKey = Depends(require_api_key)
