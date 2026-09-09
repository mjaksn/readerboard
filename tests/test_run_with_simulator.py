"""Check where scripts/run_with_simulator.py takes the API key from.

Why this file exists
====================

The launcher hands the service an environment it builds itself, and it used to
set ``READERBOARD_API_KEY`` from its own option unconditionally. A key already
set on the machine was therefore thrown away, quietly and with no way to tell:
the service came up, answered, and refused every write carrying the key its
owner had every reason to think was in use. The only lever left was the option,
which in an editor means writing the key into a launch configuration, and those
are tracked, shared, and rewritten in place by an editor that drops the comments
explaining them.

So the order below is the fix, and it is the whole of what this checks: the
option beats the environment, the environment beats the development default, and
an empty variable counts as unset rather than as a key nobody can guess.

The script is not importable as a module. It lives in ``scripts/`` with no
package around it, which is why this loads it by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "scripts" / "run_with_simulator.py"


@pytest.fixture(scope="module")
def launcher() -> ModuleType:
    """Load the launcher by path, since ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location("run_with_simulator", LAUNCHER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed so that anything in it importing itself
    # gets this module rather than a second copy.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_environment_is_used_when_no_option_is_given(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.API_KEY_VARIABLE, "a-key-this-machine-owns")
    args = launcher.build_parser().parse_args([])
    assert args.api_key == "a-key-this-machine-owns"


def test_the_option_beats_the_environment(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(launcher.API_KEY_VARIABLE, "a-key-this-machine-owns")
    args = launcher.build_parser().parse_args(["--api-key", "the-one-asked-for"])
    assert args.api_key == "the-one-asked-for"


def test_the_default_is_used_when_the_environment_says_nothing(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(launcher.API_KEY_VARIABLE, raising=False)
    args = launcher.build_parser().parse_args([])
    assert args.api_key == launcher.DEFAULT_API_KEY


def test_an_empty_variable_counts_as_unset(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty key configures the service with no key at all, which makes every
    # write a 503. Falling back to the development default is the useful
    # reading, and it is what the service does with an empty setting too.
    monkeypatch.setenv(launcher.API_KEY_VARIABLE, "")
    args = launcher.build_parser().parse_args([])
    assert args.api_key == launcher.DEFAULT_API_KEY


def test_the_help_does_not_print_the_key_in_use(
    launcher: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --help is the one output that gets pasted into a message to somebody else,
    # so the option that reads a secret out of the environment must not echo it.
    monkeypatch.setenv(launcher.API_KEY_VARIABLE, "a-key-this-machine-owns")
    assert "a-key-this-machine-owns" not in launcher.build_parser().format_help()
