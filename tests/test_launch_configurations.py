"""Check that both editors' launch configurations are files the editor can load.

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

The second half pins the one thing the two editors have to agree about, which
is that every configuration starting the service asks it to open the
documentation page. That is a setting the service reads, not something either
editor does, so the only thing holding the two files together is this test. A
configuration that quietly lost the variable would still run perfectly and just
stop opening a tab, which is precisely the kind of thing nobody files a bug
about.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from readerboard import names as service_names

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_CONFIGURATIONS = REPO_ROOT / ".idea" / "runConfigurations"
VSCODE_LAUNCH = REPO_ROOT / ".vscode" / "launch.json"

# The variable that asks the service to show its own documentation.
OPEN_DOCS = "READERBOARD_OPEN_DOCS"

# What a configuration runs when it runs the service. The launcher counts: it
# starts the service as a child and hands the whole environment down, so the
# variable set on it reaches the service the same way.
LAUNCHER = "run_with_simulator.py"


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


# ===========================================================================
# Opening the documentation, in both editors.
# ===========================================================================


def _vscode_configurations() -> list[dict]:
    """Read the VSCode configurations out of a file that is JSON with comments.

    Comments in that file are always whole lines, which is what makes dropping
    them by prefix safe: a ``//`` inside a string, in a URL for instance, is
    never at the start of one. If that ever stops being true this stops being
    right, and the parse below is what will say so.
    """
    kept = [
        line
        for line in VSCODE_LAUNCH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("//")
    ]
    try:
        return json.loads("\n".join(kept))["configurations"]
    except json.JSONDecodeError as error:
        pytest.fail("%s is not loadable: %s" % (VSCODE_LAUNCH.name, error))


def _vscode_starts_the_service(configuration: dict) -> bool:
    """Whether a VSCode configuration ends up with the service running."""
    if configuration.get("module") == service_names.IDENTIFIER:
        return True
    return LAUNCHER in configuration.get("program", "")


def _pycharm_starts_the_service(configuration: ET.Element) -> bool:
    """Whether a PyCharm configuration ends up with the service running."""
    script = configuration.find("./option[@name='SCRIPT_NAME']")
    target = "" if script is None else (script.get("value") or "")
    module_mode = configuration.find("./option[@name='MODULE_MODE']")
    as_module = module_mode is not None and module_mode.get("value") == "true"
    if as_module and target == service_names.IDENTIFIER:
        return True
    return LAUNCHER in target


def _pycharm_env(configuration: ET.Element) -> dict[str, str]:
    """Read the environment a PyCharm configuration sets."""
    return {
        env.get("name", ""): env.get("value", "")
        for env in configuration.findall("./envs/env")
    }


def test_the_vscode_file_is_loadable() -> None:
    # Nothing validates it on the way in, and VSCode reports a broken one only
    # when the run list is opened.
    assert _vscode_configurations()


def test_every_vscode_configuration_that_starts_the_service_opens_the_documentation():
    starting = [c for c in _vscode_configurations() if _vscode_starts_the_service(c)]
    assert starting, "no VSCode configuration appears to start the service"
    for configuration in starting:
        env = configuration.get("env", {})
        assert env.get(OPEN_DOCS) == "1", (
            "%r starts the service without asking it to open the documentation"
            % configuration["name"]
        )


def test_every_pycharm_configuration_that_starts_the_service_opens_the_documentation():
    starting = []
    for path in run_configuration_files():
        configuration = ET.parse(path).getroot().find("configuration")
        assert configuration is not None
        if _pycharm_starts_the_service(configuration):
            starting.append((path, configuration))

    assert starting, "no PyCharm configuration appears to start the service"
    for path, configuration in starting:
        assert _pycharm_env(configuration).get(OPEN_DOCS) == "1", (
            "%s starts the service without asking it to open the documentation"
            % path.name
        )


def test_a_configuration_that_starts_no_service_does_not_ask_for_a_browser():
    """The simulator on its own, and the tests, have no documentation to open.

    This is the other half of the pair above. Without it, the honest way to
    make those tests pass is to put the variable in every configuration, which
    would open a browser when running the test suite.
    """
    for configuration in _vscode_configurations():
        if not _vscode_starts_the_service(configuration):
            assert OPEN_DOCS not in configuration.get("env", {}), configuration["name"]

    for path in run_configuration_files():
        configuration = ET.parse(path).getroot().find("configuration")
        assert configuration is not None
        if not _pycharm_starts_the_service(configuration):
            assert OPEN_DOCS not in _pycharm_env(configuration), path.name


def test_the_two_editors_start_the_service_the_same_number_of_ways() -> None:
    """A configuration added to one editor and not the other is the usual drift.

    Both files are maintained by hand and the names match deliberately, so a
    count that disagrees means one of them gained a way to start the service
    that the other does not have.
    """
    vscode = sorted(
        c["name"] for c in _vscode_configurations() if _vscode_starts_the_service(c)
    )
    pycharm = sorted(
        ET.parse(path).getroot().find("configuration").get("name")  # type: ignore[union-attr]
        for path in run_configuration_files()
        if _pycharm_starts_the_service(ET.parse(path).getroot().find("configuration"))  # type: ignore[arg-type]
    )
    # Not equality: VSCode carries "against the real sign", which needs a config
    # file naming a serial port and has never had a PyCharm twin.
    assert set(pycharm) <= set(vscode), (
        "PyCharm starts the service in a way VSCode does not: %s"
        % sorted(set(pycharm) - set(vscode))
    )
