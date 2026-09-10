#!/usr/bin/env python3
"""Run the service against a real sign, with the client beside it.

    python scripts/run_against_a_sign.py --serial-url socket://192.168.2.154:4001
    python scripts/run_against_a_sign.py

Two processes, one command, and no simulator: the sign is the real one on the
end of a cable or an Ethernet to RS-232 adapter. The service is started first,
the client is started once the port answers, and Ctrl+C stops both. Closing the
client leaves the service running, because a closed client is only closed.

Where the sign's address comes from
===================================

``--serial-url`` is the whole of it, and the launch configurations in both
editors put it on the command line so that the address sits in the Parameters
field of the run configuration dialog, which is the one place a person editing
a run configuration is already looking. Changing which sign this talks to is
editing that field.

The address is a pyserial URL:

    socket://192.168.2.154:4001   an Ethernet to RS-232 adapter, raw TCP
    rfc2217://192.168.2.154:23    an adapter speaking the telnet serial protocol
    COM3                          a cable on Windows
    /dev/ttyUSB0                  a cable on Linux

There is no slash between the host and the port. ``socket://host/:4001`` looks
close enough to right and is not, and pyserial answers it with a bare
``TypeError`` naming neither the setting nor the value, so the URL is checked
here and the answer says which part of it is wrong.

Given no ``--serial-url``, the address is read from ``config.local.toml`` at the
root of the checkout, which ``.gitignore`` covers and which this writes on the
first run with a generated API key and a placeholder address. That file is also
where the API key comes from either way, because a key on a command line is a
key in a tracked file and in a shell history. There is always a file by the time
the service starts, so there is always a key: writing one is the first thing
done when there is none, and the key in it is generated rather than a default
anybody could guess.

The state file, and the one dangerous operation
===============================================

Writing a memory configuration erases every message on the sign, so the service
does it only when the pool described by ``slot_count`` and ``slot_capacity``
differs from the plan recorded in its state file. No state file means no record,
which reads as a change, which erases the sign.

That is correct and expected on the first run against a sign this machine has
never driven: the sign has to be allocated before anything can be written to it.
It must not happen on the second. So, unlike ``scripts/run_with_simulator.py``,
this never discards the state file.

It also keeps a state file of its own, ``.local-sign-state.json``, rather than
the ``.local-state.json`` the simulator runs use. Two reasons, and both of them
end with an erased sign. The simulator starts empty every time and the launcher
beside this one deletes that file on every run, so sharing it would mean a
simulator session quietly discarding the real sign's record. And the record a
simulator session leaves behind describes files a real sign never allocated,
which the service would then believe and write into.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import tomllib
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

# ``scripts/`` is not a package, so the half this shares with
# ``run_with_simulator.py`` has to be found by path before it can be imported.
# ``tools/signsim/run.py`` does the same thing for the same reason.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _supervise  # noqa: E402 (the path has to be set up first)

_CLIENT = _ROOT / "tools" / "apiclient" / "run.py"

# The ignored file holding this machine's key, and the sign's address when the
# command line does not carry one. Not having it is not a reason to stop when an
# address was given: one is written, with a key generated into it, and the run
# carries on.
CONFIG_FILE = _ROOT / "config.local.toml"

# Not the ``.local-state.json`` the simulator runs use. The docstring above says
# why, and the short version is that sharing it erases the sign.
DEFAULT_STATE = _ROOT / ".local-sign-state.json"

DEFAULT_API_PORT = 5001

# The key the service reads. Ordinarily it comes out of the config file; a
# machine that already exports one for other reasons wins over that, because the
# service reads the environment ahead of the file and this only reports what it
# finds.
API_KEY_VARIABLE = "READERBOARD_API_KEY"

# What the config file is written with when there is none. The address is a
# placeholder and is meant to be wrong: with no --serial-url to use instead, the
# run stops after writing it so that it gets corrected rather than being sent at
# whatever happens to be at that address.
PLACEHOLDER_SERIAL_URL = "socket://192.168.2.51:4001"

_STARTER_CONFIG = '''# readerboard, pointed at the sign attached to this machine.
#
# Written by scripts/run_against_a_sign.py and read by it. .gitignore covers this
# file and it must stay that way: the key below can write to the sign.
#
# Every setting packaging/config.example.toml documents can go here, and the
# defaults cover the rest. These are the ones worth having in front of you.

# The sign's address, as a pyserial URL. There is no slash between the host and
# the port.
#
#   socket://192.168.2.154:4001   an Ethernet to RS-232 adapter, raw TCP
#   rfc2217://192.168.2.154:23    an adapter speaking the telnet serial protocol
#   COM3                          a cable on Windows
#   /dev/ttyUSB0                  a cable on Linux
#
# The launch configurations in both editors pass the address on the command
# line instead, so that it can be edited in the run configuration dialog. This
# is what they fall back to, and what a bare run of the script uses.
serial_url = "%(serial_url)s"

# Generated when this file was written. Every write to the service carries it in
# an X-API-Key header, and the client asks for it at the top of its window. It is
# kept here rather than on a command line, because a launch configuration is a
# tracked file and a command line is a shell history.
api_key = "%(api_key)s"

# Where the service records the memory configuration it applied, so that it
# reallocates the sign only when the pool below changes. The default is an
# installed service's path, /var/lib/readerboard/state.json, which is not a
# checkout's and on Windows resolves under C:\\var.
state_path = ".local-sign-state.json"

# Changing either of these reallocates the sign's memory on the next start, and
# that ERASES every message on it. Eight messages of 256 bytes is the default.
#slot_count = 8
#slot_capacity = 256

# The sign's clock is set at startup, on this interval, and whenever the link
# comes back. Unset means this machine's own local time.
#timezone = "America/New_York"
'''


def build_parser() -> argparse.ArgumentParser:
    """Build the command line."""
    parser = argparse.ArgumentParser(
        prog="run_against_a_sign.py",
        description=(
            "Start the readerboard service against a real sign, with the client "
            "beside it. The sign's address is given here so that it can be edited "
            "in an editor's run configuration dialog; the API key comes from "
            "config.local.toml, which the repository does not track."
        ),
        epilog=(
            "Ctrl+C stops everything. Unlike run_with_simulator.py this never "
            "discards the state file, because a service with no record of the "
            "sign's memory configuration writes a new one, and that erases every "
            "message on the sign."
        ),
    )
    parser.add_argument(
        "--serial-url",
        default=None,
        help="the sign's address, and the setting to change to talk to a "
        "different sign. A pyserial URL: socket://host:port for an Ethernet to "
        "RS-232 adapter, rfc2217://host:port for one speaking the telnet serial "
        "protocol, or a device such as COM3 or /dev/ttyUSB0 for a cable. There is "
        "no slash between the host and the port. Read from config.local.toml when "
        "this is not given",
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help="port the service listens on for HTTP, which is not the sign's port "
        "(default: %d). Set here rather than read from the config file, so that "
        "the client is always pointed at the one in use" % DEFAULT_API_PORT,
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE,
        help="where the service keeps its record of what is on the sign "
        "(default: %s). It is never discarded: a missing one means the sign is "
        "reallocated on the next start, which erases it" % DEFAULT_STATE.name,
    )
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        help="the service's log level (default: DEBUG)",
    )
    parser.add_argument(
        "--no-client",
        action="store_true",
        help="run the service on its own. Starting the client writes the base URL "
        "into the settings it remembers between runs, which is as much a reason to "
        "skip it as not wanting the window",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Start them, stream them, stop them."""
    args = build_parser().parse_args(argv)

    if not args.no_client and not _supervise.require_qt(
        "the client cannot start", ["tools/apiclient/requirements.lock"]
    ):
        return 1

    settings = _read_config()
    if settings is None:
        # No config file. With an address on the command line that is no reason
        # not to run, so one is written for the key and the run carries on. With
        # no address either there is nothing to talk to, so it stops and says
        # which field to fill in.
        settings = _write_starter_config(stopping=not args.serial_url)
        if not args.serial_url:
            return 1

    serial_url = args.serial_url or str(settings.get("serial_url", ""))
    problem = _url_problem(serial_url)
    if problem is not None:
        print("[run] %s" % problem, file=sys.stderr)
        return 1

    listen_host = os.environ.get("READERBOARD_HOST") or str(
        settings.get("host", "")
    ) or "0.0.0.0"
    if _supervise.port_in_use(listen_host, args.api_port):
        # Worth checking before starting anything rather than reading it out of
        # uvicorn's log afterwards. The service would bind, fail and stop, but
        # not before the port answered a connection from whatever else holds it,
        # which is what the client would then have been pointed at.
        print(
            "[run] %s:%d is already in use, so the service cannot bind it. Stop "
            "whatever holds it, or change the api-port option in the run "
            "configuration. A second checkout of this repository running its own "
            "service is the usual answer" % (listen_host, args.api_port),
            file=sys.stderr,
        )
        return 1

    service = _start_service(args, serial_url)
    base_url = "http://%s:%d" % (_api_host(), args.api_port)
    print("[run] service on %s, documentation at /docs" % base_url)
    print("[run] sign at %s" % serial_url)
    _report_key(settings)

    children = {"api": service}
    streams = [_supervise.stream(service.stdout, "api")]

    if not args.no_client:
        # Once the port answers rather than once the process starts. The client
        # asks an address for its /openapi.json the first time that address says
        # anything, and a sign that is slow to open, or not there at all, is
        # time the service spends before it binds.
        if not _supervise.wait_for_port(_api_host(), args.api_port, service):
            print(
                "[run] the service never answered on %s, so the client was not "
                "started" % base_url,
                file=sys.stderr,
            )
            _supervise.stop(service)
            return 1
        client = _start_client(base_url)
        children["client"] = client
        streams.append(_supervise.stream(client.stdout, "client"))
        print("[run] client pointed at %s" % base_url)

    print("[run] Ctrl+C stops everything")

    return _supervise.wait(children, streams, fatal=frozenset({"api"}))


# ===========================================================================
# The file that holds the key, and the sign when the command line does not.
# ===========================================================================


def _read_config() -> dict[str, object] | None:
    """Read the local config file, or None when there is not one yet."""
    if not CONFIG_FILE.exists():
        return None
    try:
        with CONFIG_FILE.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as err:
        print(
            "[run] %s could not be read: %s" % (CONFIG_FILE.name, err), file=sys.stderr
        )
        raise SystemExit(1) from err


def _write_starter_config(stopping: bool) -> dict[str, object]:
    """Write the first config.local.toml, and return what it now holds.

    ``stopping`` says whether the run is about to give up for want of an
    address. It changes only what is printed, and it changes it a lot: the words
    that tell somebody to go and fill in a field would be nonsense above a
    service that then started perfectly well on the address it was given.
    """
    key = secrets.token_urlsafe(24)
    CONFIG_FILE.write_text(
        _STARTER_CONFIG % {"serial_url": PLACEHOLDER_SERIAL_URL, "api_key": key},
        encoding="utf-8",
    )
    if stopping:
        print(
            "[run] wrote %s with a generated API key.\n"
            "[run] Its serial_url is the placeholder %s, and no address was given\n"
            "[run] on the command line either, so there is no sign to talk to.\n"
            "[run] Put the sign's own address in the run configuration, or in that\n"
            "[run] file, and run this again. There is no slash between the host and\n"
            "[run] the port: socket://192.168.2.154:4001, not\n"
            "[run] socket://192.168.2.154/:4001."
            % (CONFIG_FILE.name, PLACEHOLDER_SERIAL_URL)
        )
    else:
        print(
            "[run] wrote %s with a generated API key, since there was none. The\n"
            "[run] address below came from the command line, so the placeholder in\n"
            "[run] that file is not used." % CONFIG_FILE.name
        )
    return {"serial_url": PLACEHOLDER_SERIAL_URL, "api_key": key}


def _report_key(settings: dict[str, object]) -> None:
    """Say which key the client will need, and where it came from.

    The client asks for the key itself rather than taking one from a command
    line, so it has to be pasted in, and reading it out of a file by hand is the
    one bit of friction there is no reason to keep.
    """
    # The service reads the environment ahead of the file, so this reports them
    # in the same order rather than reporting the file alone.
    from_environment = os.environ.get(API_KEY_VARIABLE) or ""
    if from_environment:
        print(
            "[run] the API key to paste is %s, from %s"
            % (from_environment, API_KEY_VARIABLE)
        )
        return

    from_file = settings.get("api_key") or ""
    if from_file:
        print("[run] the API key to paste is %s, from %s" % (from_file, CONFIG_FILE.name))
        return

    print(
        "[run] no api_key is set in %s, so every write will answer 503. Reads and "
        "/health still work" % CONFIG_FILE.name,
        file=sys.stderr,
    )


def _url_problem(url: str) -> str | None:
    """Say what is wrong with a serial URL, or None when nothing is.

    Only the network forms are checked, and only for shape. Whether anything
    answers at that address is the service's business, and it says so in the log
    and keeps retrying, which is what you want when the adapter is on the end of
    a network.
    """
    if not url:
        return (
            "no serial_url is set in %s and none was given with --serial-url, so "
            "there is no sign to talk to" % CONFIG_FILE.name
        )

    scheme, separator, rest = url.partition("://")
    if not separator:
        # A device path or a COM port. Nothing to check here that is not the
        # operating system's answer anyway.
        return None
    if scheme not in ("socket", "rfc2217"):
        # loop:// takes no host, and an unknown scheme is pyserial's to reject:
        # it names the handlers it has, which is more use than a list repeated
        # here and kept up to date by nobody.
        return None

    host, colon, port = rest.rpartition(":")
    if not colon or not port.isdigit() or not host or "/" in host:
        return (
            "%r is not a usable serial URL. A sign on the network is "
            "%s://host:port with no slash between them, such as %s"
            % (url, scheme, PLACEHOLDER_SERIAL_URL)
        )
    return None


# ===========================================================================
# Starting the two of them.
# ===========================================================================


def _start_service(args: argparse.Namespace, serial_url: str) -> subprocess.Popen[str]:
    """Start the service, reading the local config file an installed one would."""
    env = _supervise.child_env()
    env.update(
        {
            # The config file carries the key. What is set here is what running
            # from a checkout needs and an installed service does not: the sign
            # named in the run configuration, a state path that exists on this
            # machine, a port the client can be told about, and a log level worth
            # watching.
            "READERBOARD_CONFIG_FILE": str(CONFIG_FILE),
            "READERBOARD_SERIAL_URL": serial_url,
            "READERBOARD_STATE_PATH": str(args.state_path),
            "READERBOARD_PORT": str(args.api_port),
            "READERBOARD_LOG_LEVEL": args.log_level,
        }
    )
    # Deliberately not set: the API key. It is in the config file named above,
    # generated there when there was none, and the service reads that file
    # itself. Setting one here would override a key deliberately blanked, whose
    # meaning to the service is that every write answers 503.
    #
    # Deliberately not set either: inter_packet_delay, clock_sync_enabled and
    # settle_delays_enabled. The simulator launcher turns all three down because
    # there is no serial line to be gentle with, no clock to set, and nothing
    # that goes deaf. There is a real one of each here, so the defaults stand
    # and the config file decides. The settle in particular must stay on: it is
    # the wait that keeps a write from landing on a sign that cannot hear it.

    return subprocess.Popen(
        [sys.executable, "-m", "readerboard"],
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )


def _start_client(base_url: str) -> subprocess.Popen[str]:
    """Start the client, pointed at the service that is already answering."""
    return subprocess.Popen(
        [sys.executable, str(_CLIENT), "--base-url", base_url],
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_supervise.child_env(),
    )


def _api_host() -> str:
    """Work out the host to reach the service on."""
    configured = os.environ.get("READERBOARD_HOST", "")
    if configured in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return configured


if __name__ == "__main__":
    raise SystemExit(main())
