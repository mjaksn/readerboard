"""Building the application and wiring its parts together.

Startup order matters. The transport is opened first, then the memory
configuration is settled, then the slots and any alert are put back, and only
then does anything start on a timer. Getting that wrong would mean writing
messages to files the sign has not allocated.

Nothing here fails to start because the sign is unreachable. A service that
refused to boot with the sign unplugged would need someone to notice and restart
it once the sign came back, which is precisely the situation it exists to
survive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from readerboard import __version__, logging_setup, names
from readerboard.api import errors, routes
from readerboard.api.deps import get_alerts, get_clock, get_controller, get_registry
from readerboard.api.models import HealthResponse, LinkHealth
from readerboard.config import Settings
from readerboard.services.alerts import AlertService
from readerboard.services.clock import ClockService
from readerboard.services.registry import MessageRegistry
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
from readerboard.sign.state import StateStore
from readerboard.transport.base import Transport, TransportError
from readerboard.transport.serial_link import SerialTransport

logger = logging.getLogger(__name__)

DESCRIPTION = """
Drives a BetaBrite Classic sign over the Alpha protocol, either through a serial
cable or through an Ethernet to RS-232 adapter.

Several sources can share the sign at once. Each registers a named **slot**, and
the sign rotates through the registered slots by itself. An **alert** takes the
whole display over until it is released, then the rotation resumes.

Every write needs an `X-API-Key` header. Reads and `GET /health` do not. On this
page, put the key in once with the **Authorize** button and every write below
carries it.

A failure is reported by the status code, with the reason in a `detail` field:
400 for a command the sign does not have, a parameter it will not accept, a
message too long for its slot or markup the sign cannot render, 401 for a
missing or wrong `X-API-Key`, 404 for a slot nothing has registered, 409 when
every message slot is already in use, 503 when the sign is unreachable or no
API key is configured at all, 500 for something the service has no code for,
and 422 for a body that is not the shape the endpoint declares, which includes
a display mode or a text position the sign does not have.
"""


def build_transport(settings: Settings) -> Transport:
    """Create the link to the sign described by the settings."""
    return SerialTransport(
        settings.serial_url,
        baud_rate=settings.baud_rate,
        timeout=settings.serial_timeout,
        backoff_initial=settings.backoff_initial,
        backoff_max=settings.backoff_max,
    )


async def _refresh_loop(app: FastAPI, interval: float) -> None:
    """Push everything to the sign again, periodically.

    See ``MessageRegistry.refresh`` for why blind re-pushing is the only thing
    that repairs a sign power cycled behind a still-connected adapter.
    """
    registry: MessageRegistry = app.state.registry
    alerts: AlertService = app.state.alerts

    while True:
        await asyncio.sleep(interval)
        try:
            await registry.refresh()
            # The refresh puts the slots back, but an alert lives in the
            # priority file, which the registry does not touch. Without this a
            # sign power cycled mid-alert would stay blank until the alert's
            # deadline, and an alert with no deadline would stay blank for good.
            await alerts.reassert()
        except TransportError as err:
            logger.debug("periodic refresh skipped, sign unreachable: %s", err)
        except Exception:
            logger.exception("the periodic refresh failed")


async def _sweep_loop(app: FastAPI, interval: float) -> None:
    """Expire slots and alerts whose deadlines have passed."""
    registry: MessageRegistry = app.state.registry
    alerts: AlertService = app.state.alerts

    while True:
        await asyncio.sleep(interval)
        try:
            await alerts.sweep()
            await registry.sweep()
        except TransportError as err:
            logger.warning("could not apply expiries: %s", err)
        except Exception:
            # A sweep that raises must not take the loop down with it, or
            # nothing would ever expire again.
            logger.exception("the expiry sweep failed")


def create_app(settings: Settings | None = None, transport: Transport | None = None) -> FastAPI:
    """Build the application.

    ``transport`` is for tests, which supply a capturing one. In the service it
    is left unset and built from the settings.
    """
    settings = settings or Settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging_setup.configure(settings.log_level, settings.log_file)
        logger.info("readerboard %s starting; sign at %s", __version__, settings.serial_url)

        link = transport if transport is not None else build_transport(settings)
        controller = SignController(link, inter_packet_delay=settings.inter_packet_delay)
        store = StateStore(settings.state_path)
        state = store.load()
        layout = Layout(settings.slot_count, settings.slot_capacity)
        alerts = AlertService(controller, store, state)
        registry = MessageRegistry(
            controller, layout, store, state, alert_active=lambda: alerts.active is not None
        )
        # An alert holding the sign makes the registry hold back run sequence
        # writes; releasing it is what lets them through.
        alerts.set_release_hook(registry.flush_deferred)
        clock = ClockService(
            controller,
            interval_seconds=settings.clock_sync_interval_seconds,
            timezone=settings.timezone,
        )

        app.state.settings = settings
        app.state.controller = controller
        app.state.registry = registry
        app.state.alerts = alerts
        app.state.clock = clock

        await controller.start()

        # The sign may be unreachable, and that is not a reason to refuse to
        # start. What is put back below happens again on the next reconnect.
        try:
            await registry.restore()
            await alerts.restore()
            if settings.clock_sync_enabled:
                await clock.sync_quietly()
        except TransportError as err:
            logger.warning("could not restore the sign's contents yet: %s", err)

        # Only now, so that opening the link above does not fire hooks that
        # duplicate the work just done. Registering them earlier meant every
        # startup set the clock twice and sent a run sequence for an empty
        # registry before the pool had even been allocated.
        if settings.clock_sync_enabled:
            controller.on_reconnect(clock.sync_quietly)
        # A link that just came back may be in front of a sign that was power
        # cycled, so nothing the controller believes about its contents holds.
        controller.on_reconnect(registry.refresh)

        if settings.clock_sync_enabled:
            await clock.start()
        sweeper = asyncio.create_task(_sweep_loop(app, settings.registry_sweep_seconds))
        refresher = asyncio.create_task(
            _refresh_loop(app, settings.refresh_interval_seconds)
        )

        try:
            yield
        finally:
            for task in (sweeper, refresher):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if settings.clock_sync_enabled:
                await clock.stop()
            await controller.stop()
            logger.info("readerboard stopped")

    app = FastAPI(
        title=names.DISPLAY_NAME,
        description=DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        # Keep the key entered in the Swagger UI's Authorize dialog across a page
        # reload. Without it every reload is another trip to the config file for
        # somebody trying things out, which is most of what /docs is for.
        swagger_ui_parameters={"persistAuthorization": True},
    )
    app.state.settings = settings

    _install_error_handlers(app)
    app.include_router(routes.router)

    @app.get("/health", tags=["Health"], summary="Is the service talking to the sign")
    async def health(request: Request) -> HealthResponse:
        """Report the state of the link, the slots, and the clock.

        Deliberately unauthenticated, so that a monitor can watch the sign
        without holding a key that could write to it.
        """
        controller = get_controller(request)
        registry = get_registry(request)
        alerts = get_alerts(request)
        clock = get_clock(request)

        used, total = registry.occupancy
        return HealthResponse(
            status="ok" if controller.is_connected else "degraded",
            version=__version__,
            link=LinkHealth(
                url=controller.link_description,
                connected=controller.is_connected,
                last_write_at=controller.last_write_at,
                last_error=controller.last_error,
                writes=controller.writes,
                suppressed_writes=controller.suppressed,
            ),
            slots_used=used,
            slots_total=total,
            sign_in_sync=registry.in_sync,
            alert_active=alerts.active is not None,
            clock_last_synced_at=clock.last_sync_at,
        )

    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Turn the service's own exceptions into the status codes they mean.

    Registered by walking the table in readerboard.api.errors, which is the one
    place that decides what any of them means.
    """

    def handler(code: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handle(_: Request, exc: Exception) -> JSONResponse:
            if code >= 500:
                logger.warning("request failed: %s", exc)
            return JSONResponse(status_code=code, content={"detail": str(exc)})

        return handle

    for kind, code in errors.STATUS_FOR_ERROR:
        app.add_exception_handler(kind, handler(code))


app = create_app()
