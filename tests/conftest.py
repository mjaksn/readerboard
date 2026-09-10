"""Shared fixtures: a sign that is not there, and a clock that does not move."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from readerboard.services.alerts import AlertService
from readerboard.services.registry import MessageRegistry
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
async def registry(controller, layout, store, state, clock) -> MessageRegistry:
    # restore() is what the service calls at startup, and it is what establishes
    # the memory configuration. Skipping it here would leave every test running
    # against a sign that was never allocated.
    registry = MessageRegistry(controller, layout, store, state, now=clock)
    await registry.restore()
    return registry


@pytest.fixture
def alerts(controller, store, state, clock) -> AlertService:
    return AlertService(controller, store, state, now=clock)
