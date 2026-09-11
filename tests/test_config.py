"""The settings that decide how much of the sign's memory the service claims.

Both pools, TEXT files for messages and STRING files for variables, come out of
one memory budget, and a configuration that asks for more than it is refused at
startup rather than sent to a sign that would have to make do.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from readerboard.config import SIGN_MEMORY_BUDGET, Settings
from readerboard.protocol.constants import FILE_OVERHEAD_BYTES


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


def test_the_variables_count_against_the_budget() -> None:
    # Slots alone that fit exactly, so the variables are what tips it over.
    slot_capacity = SIGN_MEMORY_BUDGET // 26 - FILE_OVERHEAD_BYTES
    settings(slot_count=26, slot_capacity=slot_capacity, variable_count=0)
    with pytest.raises(ValidationError, match="variable_count 1"):
        settings(slot_count=26, slot_capacity=slot_capacity, variable_count=1)


def test_the_largest_variable_pool_fits_beside_the_default_slots() -> None:
    chosen = settings(variable_count=26, variable_capacity=125)
    assert chosen.variable_count == 26
