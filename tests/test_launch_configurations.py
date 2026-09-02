"""Check that every PyCharm run configuration is a file PyCharm can load.

Why this file exists
====================

PyCharm reads the shared run configurations out of ``.idea/runConfigurations/``
and, when one of them does not parse, logs an error and leaves it out of the
run list. Nothing appears on screen. The combined configuration that starts
the service, the sign simulator and the client went missing that way: its
comment spelled out the ``--with-client`` flag it passes, and XML does not
allow two hyphens in a row inside a comment. It looked exactly like the
configuration had never been committed.

The files are hand written and no editor validates them on the way in, so
this is the check. It parses each one and pins the two attributes PyCharm
needs to show it at all: a name, and the module the project is registered as.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from readerboard import names as service_names

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_CONFIGURATIONS = REPO_ROOT / ".idea" / "runConfigurations"


def run_configuration_files() -> list[Path]:
    return sorted(RUN_CONFIGURATIONS.glob("*.xml"))


def test_there_are_run_configurations_to_check() -> None:
    # A test parametrised over an empty list passes by saying nothing. Keep it
    # honest: if the directory moves, this is what fails.
    assert run_configuration_files()


@pytest.mark.parametrize("path", run_configuration_files(), ids=lambda path: path.stem)
def test_pycharm_can_parse_the_run_configuration(path: Path) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        pytest.fail(
            "%s is not well-formed XML, so PyCharm silently drops it: %s"
            % (path.relative_to(REPO_ROOT), error)
        )

    configuration = root.find("configuration")
    assert configuration is not None, "no <configuration> element"
    assert configuration.get("name"), "the configuration has no name"

    module = configuration.find("module")
    assert module is not None, "no <module> element"
    assert module.get("name") == service_names.IDENTIFIER


def test_each_configuration_name_leads_with_the_project() -> None:
    # The display name convention in AGENTS.md: a window in a taskbar says
    # which project it belongs to before it says which part.
    for path in run_configuration_files():
        name = ET.parse(path).getroot().find("configuration").get("name")  # type: ignore[union-attr]
        assert name is not None
        assert name.startswith(service_names.IDENTIFIER), name
