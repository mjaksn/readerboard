"""Check the things the real-sign launcher must not get wrong.

Why this file exists
====================

``scripts/run_against_a_sign.py`` is the one launcher pointed at hardware, and
three of its decisions are the difference between a working sign and an
afternoon spent on the wrong question.

The first is the state file. Writing a memory configuration erases every message
on the sign, and the service writes one whenever it has no record of the
configuration already applied. The launcher beside this one deletes its state
file on every launch, which is right against a simulator that starts empty and
catastrophic against a sign that does not. So this one never discards a state
file and never names the simulator's, and both of those are checked below by
reading the source rather than by running it: the failure they guard against
happens on a real sign, once, and cannot be rehearsed.

The second is the serial URL. ``socket://host/:4001`` looks close enough to
right, and pyserial answers it with a bare ``TypeError`` that names neither the
setting nor the value, from deep inside a connection attempt. That one is
checked properly, because it is ordinary logic with an ordinary answer.

The third is the port. Two checkouts of this repository on one machine both
default to 5001, and the second to start would otherwise bind, fail, and stop,
but not before the port had answered a connection from the first and the client
had been pointed at it.

The script is not importable as a module. It lives in ``scripts/`` with no
package around it, which is why this loads it by path.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
LAUNCHER = SCRIPTS / "run_against_a_sign.py"
SIMULATOR_LAUNCHER = SCRIPTS / "run_with_simulator.py"


def _load(name: str, path: Path) -> ModuleType:
    """Load a launcher by path, since ``scripts/`` is not a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before it is executed so that anything in it importing itself
    # gets this module rather than a second copy.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def launcher() -> ModuleType:
    return _load("run_against_a_sign", LAUNCHER)


# ===========================================================================
# The state file, which is the one that erases a sign.
# ===========================================================================


def test_it_keeps_a_state_file_the_simulator_runs_do_not_touch(
    launcher: ModuleType,
) -> None:
    simulator = _load("run_with_simulator", SIMULATOR_LAUNCHER)
    assert launcher.DEFAULT_STATE != simulator.DEFAULT_STATE, (
        "the real sign and the simulator share a state file, so a simulator run "
        "discards the sign's record and the next real run erases the sign"
    )


def test_it_never_discards_the_state_file() -> None:
    # By reading the source, because the thing being guarded against is a delete
    # that only matters against hardware. A test that ran the launcher would
    # need a sign to prove anything.
    source = LAUNCHER.read_text(encoding="utf-8")
    for removal in (".unlink(", "os.remove", "shutil.rmtree"):
        assert removal not in source, (
            "%s removes a file. A missing state file means the sign is "
            "reallocated on the next start, which erases every message on it"
            % LAUNCHER.name
        )


def test_the_state_path_is_inside_the_checkout(launcher: ModuleType) -> None:
    # The service defaults to /var/lib/readerboard/state.json, which on Windows
    # resolves under C:\var and on Linux is a path a checkout cannot write. A
    # state file the service cannot write is a sign reallocated on every start.
    assert launcher.DEFAULT_STATE.parent == LAUNCHER.parent.parent


# ===========================================================================
# The serial URL, which is the one that wastes an afternoon.
# ===========================================================================


@pytest.mark.parametrize(
    "url",
    [
        "socket://192.168.2.154:4001",
        "rfc2217://192.168.2.154:23",
        "socket://sign.local:4001",
        "loop://",
        "COM3",
        "/dev/ttyUSB0",
    ],
)
def test_a_usable_url_is_accepted(launcher: ModuleType, url: str) -> None:
    assert launcher._url_problem(url) is None, url


@pytest.mark.parametrize(
    "url",
    [
        # The one that started this. pyserial answers it with a TypeError
        # comparing an int to None, naming neither the setting nor the value.
        "socket://192.168.2.154/:23",
        "socket://192.168.2.154",
        "socket://:4001",
        "rfc2217://192.168.2.154/:23",
    ],
)
def test_a_url_that_pyserial_cannot_use_is_rejected(
    launcher: ModuleType, url: str
) -> None:
    problem = launcher._url_problem(url)
    assert problem is not None, url
    # The message has to carry the value, or it sends the reader back to a run
    # configuration to work out which of two addresses it meant.
    assert url in problem


def test_no_url_at_all_names_the_file_to_put_one_in(launcher: ModuleType) -> None:
    problem = launcher._url_problem("")
    assert problem is not None
    assert launcher.CONFIG_FILE.name in problem


def test_the_rejected_urls_really_are_the_ones_pyserial_rejects(
    launcher: ModuleType,
) -> None:
    """Pin the check against pyserial itself rather than against a belief.

    Guessing at another library's parser is how a check like this goes quietly
    wrong, so the URLs above are put through the handler that would have opened
    them. Nothing is connected: ``from_url`` parses and returns the address, and
    the failure being reproduced happens during that parse.
    """
    from serial.urlhandler import protocol_socket

    for good in ("socket://192.168.2.154:4001", "socket://sign.local:4001"):
        assert protocol_socket.Serial().from_url(good) is not None

    for bad in ("socket://192.168.2.154/:23", "socket://192.168.2.154"):
        with pytest.raises((TypeError, ValueError)):
            protocol_socket.Serial().from_url(bad)
        assert launcher._url_problem(bad) is not None


# ===========================================================================
# The address on the command line, which is where the run configuration puts it.
# ===========================================================================


def test_the_run_configurations_pass_an_address_the_launcher_accepts() -> None:
    """The address in each editor's configuration has to be one that works.

    It is the one value here a person is expected to edit, in a dialog that
    validates nothing, and a bad one would otherwise be found by starting the
    thing rather than by running the tests.
    """
    import json
    import shlex
    import xml.etree.ElementTree as ET

    launcher = _load("run_against_a_sign", LAUNCHER)
    root = Path(__file__).resolve().parent.parent

    found = []

    configuration = ET.parse(
        root / ".idea" / "runConfigurations"
        / "readerboard_against_the_real_sign_and_the_client.xml"
    ).getroot().find("configuration")
    assert configuration is not None
    parameters = configuration.find("./option[@name='PARAMETERS']")
    assert parameters is not None
    found.append(shlex.split(parameters.get("value") or ""))

    kept = [
        line
        for line in (root / ".vscode" / "launch.json").read_text(
            encoding="utf-8"
        ).splitlines()
        if not line.lstrip().startswith("//")
    ]
    for entry in json.loads("\n".join(kept))["configurations"]:
        if "run_against_a_sign.py" in entry.get("program", ""):
            found.append(entry.get("args", []))

    assert len(found) == 2, "both editors should carry the real-sign launcher"
    for argv in found:
        args = launcher.build_parser().parse_args(argv)
        assert args.serial_url, "the configuration passes no address to edit"
        assert launcher._url_problem(args.serial_url) is None, args.serial_url


def test_the_two_editors_pass_the_same_arguments() -> None:
    # They are maintained by hand and are meant to match. A sign address changed
    # in one and not the other is the drift this catches.
    import json
    import shlex
    import xml.etree.ElementTree as ET

    root = Path(__file__).resolve().parent.parent
    configuration = ET.parse(
        root / ".idea" / "runConfigurations"
        / "readerboard_against_the_real_sign_and_the_client.xml"
    ).getroot().find("configuration")
    assert configuration is not None
    parameters = configuration.find("./option[@name='PARAMETERS']")
    assert parameters is not None
    pycharm = shlex.split(parameters.get("value") or "")

    kept = [
        line
        for line in (root / ".vscode" / "launch.json").read_text(
            encoding="utf-8"
        ).splitlines()
        if not line.lstrip().startswith("//")
    ]
    vscode = [
        entry.get("args", [])
        for entry in json.loads("\n".join(kept))["configurations"]
        if "run_against_a_sign.py" in entry.get("program", "")
    ]
    assert vscode, "VSCode does not carry the real-sign launcher"
    assert pycharm == vscode[0], (pycharm, vscode[0])


# ===========================================================================
# The port, which is the one that points the client at the wrong service.
# ===========================================================================


def test_a_port_something_else_holds_is_reported_as_in_use() -> None:
    """The case that made this worth checking rather than leaving to uvicorn.

    A second checkout of this repository running its own service holds the port.
    Without the check the service starts, the port answers a connection from the
    other service, the client is pointed at that, and only then does the one
    being started fail to bind.
    """
    supervise = _load("_supervise", SCRIPTS / "_supervise.py")
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        assert supervise.port_in_use("127.0.0.1", port) is True


def test_a_free_port_is_not_reported_as_in_use() -> None:
    supervise = _load("_supervise", SCRIPTS / "_supervise.py")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    # Closed again, so nothing holds it now.
    assert supervise.port_in_use("127.0.0.1", port) is False
