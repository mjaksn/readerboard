"""The settings that decide how much of the sign's memory the service claims.

Both pools, TEXT files for messages and STRING files for variables, come out of
one memory pool, and a configuration that asks for more than a BetaBrite Classic
has is refused here rather than sent to a sign that cannot hold it.

This is the tier of the check that runs with no sign present. The other one is
in ``tests/test_pool.py``, which covers what happens when the sign is asked for
its own figure at startup.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from readerboard.config import ASSUMED_SIGN_MEMORY_POOL, Settings
from readerboard.protocol.constants import (
    MEASURED_FILE_OVERHEAD_BYTES,
    MEASURED_POOL_OVERHEAD_BYTES,
)


def settings(**overrides: object) -> Settings:
    return Settings(serial_url="loop://", **overrides)


def test_variables_are_on_by_default() -> None:
    chosen = settings()
    assert chosen.variable_count == 8
    assert chosen.variable_capacity == 32


def test_variables_can_be_turned_off() -> None:
    assert settings(variable_count=0).variable_count == 0


@pytest.mark.parametrize("capacity", [0, 126])
def test_a_variable_capacity_the_sign_cannot_hold_is_refused(capacity: int) -> None:
    with pytest.raises(ValidationError):
        settings(variable_capacity=capacity)


def test_the_variables_count_against_the_pool() -> None:
    # Slots alone that fit exactly, so the variables are what tips it over.
    room = ASSUMED_SIGN_MEMORY_POOL - MEASURED_POOL_OVERHEAD_BYTES
    slot_capacity = room // 26 - MEASURED_FILE_OVERHEAD_BYTES
    settings(slot_count=26, slot_capacity=slot_capacity, variable_count=0)
    with pytest.raises(ValidationError, match="variable_count 1"):
        settings(slot_count=26, slot_capacity=slot_capacity, variable_count=1)


def test_the_defaults_fit_the_sign_with_room_to_spare() -> None:
    # Eight messages of 256 bytes and eight variables of 32 is under half of
    # what the sign has, which is what makes it a sane default to ship.
    chosen = settings()
    claimed = (
        chosen.slot_count * (chosen.slot_capacity + MEASURED_FILE_OVERHEAD_BYTES)
        + chosen.variable_count * (chosen.variable_capacity + MEASURED_FILE_OVERHEAD_BYTES)
        + MEASURED_POOL_OVERHEAD_BYTES
    )
    assert claimed < ASSUMED_SIGN_MEMORY_POOL // 2


def test_a_pool_the_sign_could_never_hold_is_refused() -> None:
    # Twenty six slots of 800 bytes is 21138, nearly four times the pool. It was
    # accepted until the sign was asked how much memory it actually has, and the
    # only thing that noticed was the sign, by showing nothing.
    with pytest.raises(ValidationError, match="Lower one of them"):
        settings(slot_count=26, slot_capacity=800)


def test_the_largest_variable_pool_fits_beside_a_slot_pool_that_makes_room() -> None:
    # 26 variables at 125 bytes is 3588 of the 5482 on their own, so the slots
    # have to be small for the two to share. They no longer fit beside the
    # default eight slots of 256, which they did while the budget was a guess.
    chosen = settings(variable_count=26, variable_capacity=125, slot_count=4, slot_capacity=256)
    assert chosen.variable_count == 26
    with pytest.raises(ValidationError, match="variable_capacity 125"):
        settings(variable_count=26, variable_capacity=125)
