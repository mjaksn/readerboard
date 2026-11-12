"""Building the application and wiring its parts together.

Startup order matters. The transport is opened first, then the memory
configuration is settled, then the slots and any alert are put back, and only
then does anything start on a timer. Getting that wrong would mean writing
messages to files the sign has not allocated.

Nothing here fails to start because the sign is unreachable. A service that
refused to boot with the sign unplugged would need someone to notice and restart
it once the sign came back, which is precisely the situation it exists to
survive.

There is one thing that does stop it, and it is not that. A sign that is
answering and has less memory than the configured pool needs is a configuration
somebody has to change, and going ahead would erase every message on the sign to
write a pool that cannot work. See readerboard.sign.pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from readerboard import __version__, logging_setup, names
from readerboard.api import errors, routes
from readerboard.api.deps import get_alerts, get_clock, get_controller, get_registry
from readerboard.api.models import HealthResponse, LinkHealth
from readerboard.config import Settings
from readerboard.services.alerts import AlertService
from readerboard.services.clock import ClockService
from readerboard.services.registry import SlotRegistry
from readerboard.sign import pool
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
the sign rotates through the slots that are showing by itself. A slot can be
hidden without being given up, so a message can be taken off the display and put
back without being sent again. An **alert** takes the whole display over until it
is released, then the rotation resumes. An alert that is not urgent can queue
behind the one showing instead, and takes the sign when that one ends; the
rotation resumes once no alert is showing or waiting.

A **variable** is a value a slot's message or an alert calls with `<var:name>`.
Changing it rewrites only the variable, so the sign shows the new value without
blanking or restarting what calls it.

An **icon** is one of the built-in bitmaps, drawn where `<icon:name>` sits in a
message or an alert. `GET /enumerations/icons` lists them,
`GET /enumerations/icons/bitmaps` gives the dots each is drawn with, and
`GET /enumerations/icon-tints` lists the colours one that takes a tint can be
asked for. There is a fixed pool of picture files on the sign and it is far
smaller than the library, so a file is claimed by whichever icon a message calls
and kept until another icon needs it. Icons are off until `picture_count` is
raised, because raising it reallocates the sign's memory and clears it.

Every write needs an `X-API-Key` header, and so does `GET /sign/information`,
which reads the sign rather than the service: it puts a question on the wire and
holds the sign until the answer comes back. The service's own reads and
`GET /health` need none. On this page, put the key in once with the
**Authorize** button and everything below that needs it carries it.

A failure is reported by the status code, with the reason in a `detail` field:
400 for a command the sign does not have, a parameter it will not accept, a
message or value too long for its file, markup the sign cannot render, a call to
a variable that does not exist, an icon nobody has, a colour asked for on an icon
drawn in fixed colours, or an icon called while icons are switched off, 401 for a
missing or wrong `X-API-Key`, 404 for a slot, a variable or a waiting alert that
does not exist, 409 when every slot, every variable or every picture file is
already in use, a queued alert calls more different icons than the sign has
picture files, a variable something still calls is deleted, an alert is raised
with `if_active` set to `fail` while one is up or waiting, the alert queue is
full, or a reboot finds the sign has less memory than the configuration needs,
503 when the sign is unreachable, stops
partway through an answer or answers with something the service cannot read, or
no API key is configured at all, 500 for something the service has no code for,
and 422 for a body that is not the shape the endpoint declares, which includes a
display mode the sign does not have.
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


async def _refresh_and_reassert(registry: SlotRegistry, alerts: AlertService) -> None:
    """Refresh the sign, and make sure any alert is back on it.

    The refresh checks that the sign still holds what it was given, and pushes
    all of it again if not. Both of the places that refresh go through here, the
    timer below and the reconnect hook, because both are answering the same
    question: the sign may have been power cycled and nothing the controller
    believes about it holds.

    The refresh puts the slots back, and it puts the alert back too when it had
    to hand the sign over to write a picture. It says whether the alert can be
    left alone, and that is why this asks rather than always re-asserting:
    re-asserting an alert the sign already holds restarts it on the display for
    nothing, which is a visible flicker every refresh interval for as long as
    the alert is up. It can be left alone after that hand-back, and after a
    picture read that found the sign still holding what it was given, since the
    sign keeps its priority file exactly as long as it keeps everything else.
    The re-assert runs in the remaining case, a re-push that never went near the
    priority file, which is a service with no icons; there it is the only thing
    that would put the alert on a sign that came back blank. It also runs when
    the alert service says a change it made to the priority file did not
    finish, such as an alert lifted for a picture write and never put back,
    which no answer from the sign can speak for.
    """
    if not await registry.refresh() or alerts.sign_may_not_match:
        await alerts.reassert()


async def _after_reconnect(registry: SlotRegistry, alerts: AlertService) -> None:
    """Refresh a sign whose link has just come back, and let it be asked again.

    The refresh on the reconnect itself re-pushes everything without asking,
    because whatever went down the connection that died may not have arrived.
    The controller forgets what the sign holds before this runs, but this does
    not rely on that: when a write opened the link again under itself, other
    writes can fill the cache before this gets the lock. See
    ``SlotRegistry.repush_at_next_refresh``. It is every refresh after that one
    the other half is for: they read a picture back again, rather than
    re-pushing everything because a read went unanswered while the link was on
    its way down. See ``SlotRegistry.ask_the_sign_again``.
    """
    await registry.repush_at_next_refresh()
    await registry.ask_the_sign_again()
    await _refresh_and_reassert(registry, alerts)


async def _refresh_loop(app: FastAPI, interval: float) -> None:
    """Check periodically that the sign still holds what it was given.

    This is the only thing that repairs a sign power cycled behind a
    still-connected adapter, and it pushes everything again when the sign has
    lost it. See ``SlotRegistry.refresh`` for why it asks the sign first rather
    than pushing everything every time.
    """
    registry: SlotRegistry = app.state.registry
    alerts: AlertService = app.state.alerts

    while True:
        await asyncio.sleep(interval)
        try:
            await _refresh_and_reassert(registry, alerts)
        except TransportError as err:
            logger.debug("periodic refresh skipped, sign unreachable: %s", err)
        except Exception:
            logger.exception("the periodic refresh failed")


async def _sweep_loop(app: FastAPI, interval: float) -> None:
    """Expire slots and alerts, and turn variables stale, once their deadlines pass.

    And put the next waiting alert on the sign once nothing holds it, which is
    here rather than wherever an alert ends because this loop holds no lock.
    See ``AlertService.promote``.
    """
    registry: SlotRegistry = app.state.registry
    alerts: AlertService = app.state.alerts

    while True:
        await asyncio.sleep(interval)
        try:
            await alerts.sweep()
            await registry.sweep()
            await alerts.promote()
        except TransportError as err:
            logger.warning("could not apply expiries or show the next waiting alert: %s", err)
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
        controller = SignController(
            link,
            inter_packet_delay=settings.inter_packet_delay,
            settle=settings.settle_delays_enabled,
        )
        store = StateStore(settings.state_path)
        state = store.load()
        layout = Layout(
            settings.slot_count,
            settings.slot_capacity,
            settings.variable_count,
            settings.variable_capacity,
            settings.picture_count,
        )
        alerts = AlertService(controller, store, state, queue_limit=settings.alert_queue_limit)
        registry = SlotRegistry(controller, layout, store, state)
        # An alert calling a variable is rendered by the registry, under its
        # lock, so the variable cannot be deleted while the alert calls it.
        # One waiting its turn is checked under that lock too.
        alerts.set_rendering(registry.rendering)
        # And the other way: the registry hands the sign back before it writes a
        # picture, because the sign will not take one while a priority message
        # is running. Both directions are wired here rather than either service
        # reaching for the other, and the lock order is what the two of them
        # have to agree on: the registry's first, then this one's.
        registry.set_priority_hold(alerts.lifted)
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

        # Only when a reconfiguration is due, which is the one moment the pool
        # can be got wrong and the one moment the sign is about to be erased. An
        # ordinary restart asks the sign nothing, costs the display nothing, and
        # behaves exactly as it did before this check existed. Nothing here can
        # bring on a reallocation either: the only thing it can do is stop one.
        if layout.needs_reconfiguration(state.layout):
            budget = await pool.measure(controller, fallback=pool.ASSUMED_SIGN_MEMORY_POOL)
            try:
                pool.check_fits(layout, budget)
            except pool.PoolTooLarge as err:
                # Logged as well as raised, because the traceback uvicorn prints
                # around a failed startup buries the one sentence that says what
                # to do about it.
                logger.error("refusing to start: %s", err)
                await controller.stop()
                raise

        # The sign may be unreachable, and that is not a reason to refuse to
        # start. What is put back below happens again on the next reconnect.
        try:
            await registry.restore()
            await alerts.restore()
            if settings.clock_sync_enabled:
                await clock.sync_quietly()
        except TransportError as err:
            logger.warning("could not restore the sign's contents yet: %s", err)
        except Exception:
            # Nothing in a state file written by an older version should be able
            # to stop this version starting. The specific case that reached here
            # was an alert which no longer fitted the priority file once a markup
            # token it used had been removed, raising a ProtocolError, which is a
            # ValueError and so walked straight past the handler above and out of
            # the lifespan. The service then failed to start on every attempt,
            # and the only fix was editing the state file by hand on the machine.
            # Coming up with a blank sign and a logged traceback is recoverable;
            # not coming up is not.
            logger.exception(
                "could not restore the sign's contents; starting anyway. The sign may "
                "be blank until the next write or refresh."
            )

        # Only now, so that opening the link above does not fire hooks that
        # duplicate the work just done. Registering them earlier meant every
        # startup set the clock twice and sent a run sequence for an empty
        # registry before the pool had even been allocated.
        if settings.clock_sync_enabled:
            controller.on_reconnect(clock.sync_quietly)
        # A link that just came back may be in front of a sign that was power
        # cycled, so nothing the controller believes about its contents holds.
        # The alert goes back on with everything else: it was only the slots
        # before, so a reconnect to a sign that had lost an alert left the
        # priority file empty until the next tick of the timer above.
        controller.on_reconnect(functools.partial(_after_reconnect, registry, alerts))

        if settings.clock_sync_enabled:
            await clock.start()
        sweeper = asyncio.create_task(_sweep_loop(app, settings.registry_sweep_seconds))
        refresher = asyncio.create_task(
            _refresh_loop(app, settings.check_interval_seconds)
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
        variables_used, variables_total = registry.variable_occupancy
        pictures_used, pictures_total = registry.picture_occupancy
        # Both, so that a monitor reading this one field hears about a sign the
        # service knows is stale, not only about a link that is down. A write
        # that failed with the link still open is what leaves the two apart.
        healthy = controller.is_connected and registry.in_sync
        return HealthResponse(
            status="ok" if healthy else "degraded",
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
            variables_used=variables_used,
            variables_total=variables_total,
            pictures_used=pictures_used,
            pictures_total=pictures_total,
            sign_in_sync=registry.in_sync,
            alert_active=alerts.active is not None,
            alerts_waiting=len(alerts.waiting),
            clock_last_synced_at=clock.last_sync_at,
        )

    return app


def _install_error_handlers(app: FastAPI) -> None:
    """Turn the service's own exceptions into the status codes they mean.

    Registered by walking the table in readerboard.api.errors, which is the one
    place that decides what any of them means. The 422 for a body pydantic
    rejects is FastAPI's, replaced only so that it can always be encoded.
    """

    def handler(code: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handle(_: Request, exc: Exception) -> JSONResponse:
            if code >= 500:
                logger.warning("request failed: %s", exc)
            return JSONResponse(status_code=code, content={"detail": str(exc)})

        return handle

    for kind, code in errors.STATUS_FOR_ERROR:
        app.add_exception_handler(kind, handler(code))

    async def refused(_: Request, exc: Exception) -> JSONResponse:
        # FastAPI's own handler for this, with one difference. It echoes each
        # rejected value back, and a body holding Infinity, NaN or a literal too
        # big for a float, such as 1e400, has a value JSON cannot carry, so
        # encoding the 422 raised and the caller got a 500 for a body that was
        # simply wrong. Those values go back as their text instead.
        assert isinstance(exc, RequestValidationError)
        detail = _encodable(jsonable_encoder(exc.errors()))
        return JSONResponse(status_code=422, content={"detail": detail})

    app.add_exception_handler(RequestValidationError, refused)


def _encodable(value: Any) -> Any:
    """Return ``value`` with every float JSON cannot carry turned into its text."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _encodable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encodable(item) for item in value]
    return value


app = create_app()
