"""Pin what each of the three runnable components is called.

Why this file exists
====================

There are three things here that a person can run: the service, the sign
simulator and the client. Before this file, each of them answered to three or
more names and the surfaces disagreed. The simulator was the worst of it: the
directory and ``prog=`` said ``signsim``, the Qt application name said
"readerboard sign simulator" and the window title said "BetaBrite sign
simulator", so the title bar and the taskbar entry contradicted each other. The
client's window said "readerboard client" while everything else about it said
``relayclient``, a word built on a concept this project does not have and which
appeared nowhere else in the tree.

Nothing pinned any of those strings, which is why they drifted. Each component
now reads its names off a ``names`` module, and this file is what holds those
modules to the convention in ``AGENTS.md``.

The three tiers
===============

The identifier is what code, paths and settings keys use. It is the expensive
one: renaming it moves directories, import statements, lock file paths, the
``pytest`` test paths and, for the client, the QSettings location holding the
remembered base URL. The display name is what a person reads on screen or in a
unit file, and costs nothing to change. The prose name is what sentences call
it.

The last test here is the one that does the work over time. Pinning a constant
catches an edit to the constant; it does nothing about a retired name creeping
back into a README, a CI step name or a launch configuration, which is exactly
how "relay client" survived in a CI step after the rest of the tool had settled
on one word.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from readerboard import names as service_names

REPO_ROOT = Path(__file__).resolve().parent.parent

# The two tools are not installed, and unlike their own suites this file is
# collected from the repository root, so nothing has put them on the path yet.
# Both ``names`` modules import nothing at all, Qt included, so this stays safe
# in CI where PySide6 is absent.
for _tool in ("signsim", "apiclient"):
    _tool_root = str(REPO_ROOT / "tools" / _tool)
    if _tool_root not in sys.path:
        sys.path.insert(0, _tool_root)

from apiclient import names as client_names  # noqa: E402 (the path has to be set up first)
from signsim import names as simulator_names  # noqa: E402 (the path has to be set up first)

ALL_NAMES = (service_names, simulator_names, client_names)

# Names this project used to answer to, and the one it answers to now. A retired
# name is banned outright rather than merely replaced, because the cost of these
# is not the wrong word in one place. It is a reader who finds two words for one
# thing and cannot tell whether they mean the same thing.
RETIRED = {
    "relayclient": "apiclient, the client's identifier",
    "relay client": "the client, its prose name",
    "BetaBrite sign simulator": "readerboard sign simulator, the simulator's display name",
}

# CHANGELOG.md is exempt. A released entry is a record of what was true when it
# shipped, and rewriting the name in it would make the history a worse guide to
# the versions it describes, not a better one.
SKIP_FILES = {"CHANGELOG.md"}

SKIP_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
    ".venv",
    # Agent worktrees live under here, and each is another checkout of this same
    # repository. Walking into one would scan the tree twice and report a hit
    # against a path that is not the one anybody would go and edit.
    ".claude",
}


def text_files() -> list[Path]:
    """Every file in the tree a person reads, prose and code alike."""
    found = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(REPO_ROOT).parts
        if SKIP_DIRECTORIES.intersection(parts):
            continue
        if any(part.endswith(".egg-info") for part in parts):
            continue
        if path.name in SKIP_FILES or path.name == Path(__file__).name:
            continue
        found.append(path)
    return found


def test_the_service_names() -> None:
    assert service_names.IDENTIFIER == "readerboard"
    assert service_names.DISPLAY_NAME == "readerboard"
    assert service_names.PROSE_NAME == "the service"


def test_the_simulator_names() -> None:
    assert simulator_names.IDENTIFIER == "signsim"
    assert simulator_names.DISPLAY_NAME == "readerboard sign simulator"
    assert simulator_names.PROSE_NAME == "the sign simulator"


def test_the_client_names() -> None:
    assert client_names.IDENTIFIER == "apiclient"
    assert client_names.DISPLAY_NAME == "readerboard client"
    assert client_names.PROSE_NAME == "the client"


def test_the_client_settings_live_under_the_services_identifier() -> None:
    # QSettings builds its storage location out of both names. The organisation
    # is the project, not the tool, so the client and anything added later share
    # one place rather than each inventing their own.
    assert client_names.ORGANISATION == service_names.IDENTIFIER


@pytest.mark.parametrize("module", ALL_NAMES, ids=lambda module: module.IDENTIFIER)
def test_an_identifier_is_a_bare_lowercase_word(module: object) -> None:
    # It has to work as a directory name, a Python package name, an argv[0] and
    # a settings key, so it gets the intersection of what all four allow.
    identifier = module.IDENTIFIER  # type: ignore[attr-defined]
    assert identifier.isalnum()
    assert identifier.islower()


@pytest.mark.parametrize("module", ALL_NAMES, ids=lambda module: module.IDENTIFIER)
def test_a_display_name_leads_with_the_project(module: object) -> None:
    # So that a window in a taskbar, or a unit in `systemctl list-units`, says
    # which project it belongs to before it says which part of it this is.
    assert module.DISPLAY_NAME.startswith(service_names.IDENTIFIER)  # type: ignore[attr-defined]


@pytest.mark.parametrize("module", ALL_NAMES, ids=lambda module: module.IDENTIFIER)
def test_a_prose_name_is_a_noun_phrase(module: object) -> None:
    # Prose names are written to drop into a sentence as they are, which is what
    # keeps a CI step or a log line from coining a fourth spelling.
    assert module.PROSE_NAME.startswith("the ")  # type: ignore[attr-defined]
    assert module.PROSE_NAME.islower()  # type: ignore[attr-defined]


def test_the_identifiers_are_distinct() -> None:
    identifiers = [module.IDENTIFIER for module in ALL_NAMES]
    assert len(set(identifiers)) == len(identifiers)


def test_each_identifier_is_what_its_directory_is_called() -> None:
    # Half of a rename is the easy half. This is the other half.
    assert (REPO_ROOT / service_names.IDENTIFIER).is_dir()
    for module in (simulator_names, client_names):
        tool_root = REPO_ROOT / "tools" / module.IDENTIFIER
        assert tool_root.is_dir()
        assert (tool_root / module.IDENTIFIER).is_dir()


@pytest.mark.parametrize("retired", sorted(RETIRED), ids=lambda retired: retired.replace(" ", "-"))
def test_a_retired_name_does_not_come_back(retired: str) -> None:
    # Every run of whitespace collapses to one space before the comparison, so
    # that a retired name split across a line break is still caught. The prose
    # here wraps at eighty columns and two of the three names below are more
    # than one word, so matching the raw text would miss them exactly where they
    # are most likely to appear.
    offenders = []
    for path in text_files():
        try:
            contents = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        flattened = " ".join(contents.split()).lower()
        if retired.lower() in flattened:
            offenders.append(str(path.relative_to(REPO_ROOT)))

    assert not offenders, "%r is retired. Use %s. Found in: %s" % (
        retired,
        RETIRED[retired],
        ", ".join(sorted(offenders)),
    )
