"""Shared fixtures: a sign that is not there, and a clock that does not move."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from readerboard.protocol import constants as c
from readerboard.services.alerts import AlertService
from readerboard.services.registry import SlotRegistry
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
from readerboard.sign.state import ServiceState, StateStore
from readerboard.transport.fake import FakeTransport

START = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


class FrozenClock:
    """A clock the test moves by hand, so TTL tests do not sleep."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def transport() -> FakeTransport:
    return FakeTransport()


def _pool_reading(pool_size: bytes) -> bytes:
    """Build the reply a sign gives when asked how big its memory pool is."""
    body = (
        c.STX
        + c.COMMAND_WRITE_SPECIAL
        + c.SF_GENERAL_INFORMATION
        + b"1044-160B01931433M00"
        + pool_size
        + b",0BB8"
        + c.ETX
    )
    return (
        c.NUL * 20
        + c.SOH
        + c.SIGN_TYPE_RESPONSE
        + c.SIGN_ADDRESS_BROADCAST
        + body
        + b"%04X" % sum(body)
        + c.EOT
    )


@pytest.fixture
def answer_a_pool_reading(transport: FakeTransport) -> Callable[..., None]:
    """Queue the answer a sign gives when asked how big its memory pool is.

    Every path that writes a memory configuration asks first, and a fake answers
    nothing it was not told to answer, so a test that reallocates without this
    waits out the read's three second deadline and then falls back to the
    assumed figure anyway. The default is roomier than the sign this service
    drives, so the check passes and the test is about whatever it was about.
    Queue a smaller one to make the check refuse.

    A fixture rather than a function to import. Three suites are collected in
    one run and each has a ``conftest`` with no package around it, so ``from
    conftest import`` resolves to whichever one landed on the path first: it
    works when ``tests/`` is run alone and fails the moment the whole suite is.
    """

    def queue(pool_size: bytes = b"4000") -> None:
        transport.replies.append(_pool_reading(pool_size))

    return queue


async def instant_sleep(seconds: float) -> None:
    """Take no time, but still give the loop a chance to run something else.

    The yield is not decoration. A settle is taken with the sign's lock held,
    and a test that never yields inside it would let a task hold that lock from
    beginning to end, so a missing lock would look exactly like a held one.
    """
    await asyncio.sleep(0)


@pytest.fixture
def controller(transport: FakeTransport) -> SignController:
    # Settles are real durations, ten seconds in the case of a reset. The
    # controller under test keeps them switched on, because that is what a sign
    # gets; it is the waiting that is skipped, by handing it a sleep that does
    # not.
    return SignController(transport, inter_packet_delay=0, sleep=instant_sleep)


@pytest.fixture
def store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.json")


@pytest.fixture
def state() -> ServiceState:
    return ServiceState()


@pytest.fixture
def layout() -> Layout:
    return Layout(3, 256)


@pytest.fixture
async def registry(controller, layout, store, state, clock) -> SlotRegistry:
    # restore() is what the service calls at startup, and it is what establishes
    # the memory configuration. Skipping it here would leave every test running
    # against a sign that was never allocated.
    registry = SlotRegistry(controller, layout, store, state, now=clock)
    await registry.restore()
    return registry


@pytest.fixture
def alerts(controller, store, state, clock) -> AlertService:
    return AlertService(controller, store, state, now=clock)
