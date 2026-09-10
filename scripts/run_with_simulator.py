#!/usr/bin/env python3
"""Run the service and the sign simulator together, already talking to each other.

Two processes, one command. The simulator is started first and its listening
address is read back from its own output, and the service is then started with
``READERBOARD_SERIAL_URL`` pointing at it. Both sets of output are prefixed and
streamed here, and Ctrl+C stops both.

    python scripts/run_with_simulator.py
    python scripts/run_with_simulator.py --with-client

The service ends up on http://127.0.0.1:5001 with its documentation at /docs,
and every transmission it makes appears decoded in the simulator window.

``--with-client`` starts the client as well, pointed at the service, so all
three come up from one command. It is off by default because the client is a
window you may not want, and because starting it writes the base URL into the
settings the client remembers between runs, replacing whatever was there. The
client asks for the API key itself and takes none from a command line, so the
key in use is printed here for pasting. Closing the client leaves the other two
running, which closing either of them does not: the service writing to a
simulator that has gone away is broken, and a closed client is only closed.

The key itself has a development default, so writes work with nothing set up.
``READERBOARD_API_KEY`` in the environment is taken ahead of that default when
it holds anything, and ``--api-key`` beats both. That order is what lets a
machine use a key of its own: set the variable once, wherever that machine sets
variables, and every editor configuration that runs this script picks it up.
The alternative is putting the key in the launch configuration, and those are
tracked and shared, so it would be committed.

One default is worth knowing about, because getting it wrong is confusing rather
than obviously broken. The service records the memory configuration it applied
in its state file and reconfigures only when the plan changes, which is exactly
right against a sign that keeps its memory across a restart. The simulator does
not: it starts empty every time. Run the two together with a state file left
over from last time and the service, believing the sign is already configured,
sends no memory configuration at all, and the simulator then refuses every write
to any file but ``A`` because nothing has been allocated. So this uses a fresh
state file each run by default, which matches the simulator starting fresh.
``--keep-state`` opts out, for when the persistence path is the thing being
tested.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

# ``scripts/`` is not a package, so the half this shares with
# ``run_against_a_sign.py`` has to be found by path before it can be imported.
# ``tools/signsim/run.py`` does the same thing for the same reason.
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _supervise  # noqa: E402 (the path has to be set up first)

_SIMULATOR = _ROOT / "tools" / "signsim" / "run.py"
_CLIENT = _ROOT / "tools" / "apiclient" / "run.py"

DEFAULT_SIM_PORT = 4001
DEFAULT_API_PORT = 5001
DEFAULT_API_KEY = "local-development-key"

# The key the service reads, which is also where this looks before falling back to
# the development default above. Taking it from the environment is what lets a
# machine run with a key of its own: the flag below would have to be written into
# a launch configuration, and those are tracked, shared, and rewritten in place by
# an editor that drops the comment explaining them. A key on a command line is
# also a key in the shell history, which is the same reason the client has no
# option for one.
API_KEY_VARIABLE = "READERBOARD_API_KEY"
DEFAULT_STATE = _ROOT / ".local-state.json"

# What the simulator prints once it is bound, which is what says it is safe to
# start the service. Waiting on this rather than connecting to the port matters:
# a probe connection would show up in the window as a client that never sends
# anything, which is noise in the one place this tool exists to keep clean.
_LISTENING = re.compile(r"^listening on (?P<address>\S+)$")


def build_parser() -> argparse.ArgumentParser:
    """Build the command line."""
    parser = argparse.ArgumentParser(
        prog="run_with_simulator.py",
        description=(
            "Start the readerboard service and the sign simulator together, with "
            "the service already pointed at the simulator. No hardware and no "
            "configuration needed."
        ),
        epilog=(
            "Ctrl+C stops everything. The environment is passed down to both "
            "children, so READERBOARD_SLOT_COUNT and the like still work. What "
            "this script decides for itself is what its own options cover, and "
            "the option covering each of those says so."
        ),
    )
    parser.add_argument(
        "--sim-port",
        type=int,
        default=DEFAULT_SIM_PORT,
        help="port for the simulator to listen on (default: %d, 0 to let the "
        "operating system choose)" % DEFAULT_SIM_PORT,
    )
    parser.add_argument(
        "--api-port",
        type=int,
        default=DEFAULT_API_PORT,
        help="port for the service (default: %d)" % DEFAULT_API_PORT,
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get(API_KEY_VARIABLE) or DEFAULT_API_KEY,
        help="the key every write needs. Without one the service refuses them all "
        "with a 503, so this has a development default rather than being required. "
        "%s in the environment is taken when this is not given, which is how a "
        "machine uses a key of its own without writing it into a file the "
        "repository tracks. An empty value there counts as unset" % API_KEY_VARIABLE,
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE,
        help="where the service keeps its state (default: %s)" % DEFAULT_STATE.name,
    )
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        help="the service's log level (default: DEBUG)",
    )
    parser.add_argument(
        "--with-client",
        action="store_true",
        help="start the client too, pointed at the service, so all three come up "
        "from one command. It asks for the API key itself rather than taking one "
        "from a command line, so the key in use is printed for pasting. Starting "
        "it this way also writes the base URL into the settings the client "
        "remembers between runs, replacing whatever was there",
    )
    parser.add_argument(
        "--keep-state",
        action="store_true",
        help="reuse the state file instead of starting from a clean one. The "
        "simulator starts empty every run, so a state file that says the sign is "
        "already configured means no memory configuration is sent and the "
        "simulator refuses every write. Use this only when that is the point",
    )
    parser.add_argument(
        "--clock-sync",
        action="store_true",
        help="leave the hourly clock sync on. It is off by default, so the log "
        "shows what you did rather than what the timer did",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Start them, stream them, stop them."""
    args = build_parser().parse_args(argv)

    blocked = (
        "neither the simulator nor the client can start"
        if args.with_client
        else "the simulator cannot start"
    )
    # The two tools pin Qt separately, so the one that is wanted is the one
    # named. They hold the same versions today and need not tomorrow.
    locks = ["tools/signsim/requirements.lock"]
    if args.with_client:
        locks.append("tools/apiclient/requirements.lock")
    if not _supervise.require_qt(blocked, locks):
        return 1

    if not args.keep_state:
        _discard_state(args.state_path)

    simulator = _start_simulator(args.sim_port)
    if simulator is None:
        return 1

    process, address = simulator
    print("[run] simulator listening on %s" % address)

    service = _start_service(args, address)
    base_url = "http://%s:%d" % (_api_host(), args.api_port)
    print("[run] service on %s, documentation at /docs" % base_url)

    children = {"sim": process, "api": service}
    streams = [
        _supervise.stream(process.stdout, "sim"),
        _supervise.stream(service.stdout, "api"),
    ]

    if args.with_client:
        client = _start_client(base_url)
        children["client"] = client
        streams.append(_supervise.stream(client.stdout, "client"))
        print("[run] client pointed at %s, and the API key to paste is %s"
              % (base_url, args.api_key))

    print("[run] Ctrl+C stops everything")

    return _supervise.wait(children, streams, fatal=frozenset({"sim", "api"}))


# ===========================================================================
# Starting the two halves.
# ===========================================================================


def _start_simulator(port: int) -> tuple[subprocess.Popen[str], str] | None:
    """Start the simulator and wait for it to say where it is listening."""
    process = subprocess.Popen(
        [sys.executable, str(_SIMULATOR), "--port", str(port)],
        cwd=_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=_supervise.child_env(),
    )

    assert process.stdout is not None
    for line in process.stdout:
        text = line.rstrip()
        print("[sim] %s" % text)
        found = _LISTENING.match(text)
        if found is not None:
            return process, found.group("address")

    # The stream ended without that line, so it failed rather than started.
    process.wait()
    print(
        "[run] the simulator exited with status %d before it was listening"
        % process.returncode,
        file=sys.stderr,
    )
    return None


def _start_service(args: argparse.Namespace, address: str) -> subprocess.Popen[str]:
    """Start the service with everything it needs to reach the simulator."""
    env = _supervise.child_env()
    env.update(
        {
            "READERBOARD_SERIAL_URL": "socket://%s" % address,
            API_KEY_VARIABLE: args.api_key,
            "READERBOARD_STATE_PATH": str(args.state_path),
            "READERBOARD_PORT": str(args.api_port),
            "READERBOARD_LOG_LEVEL": args.log_level,
            "READERBOARD_CLOCK_SYNC_ENABLED": "true" if args.clock_sync else "false",
            # There is no serial line to be gentle with, so waiting half a second
            # between packets only makes the log arrive slowly.
            "READERBOARD_INTER_PACKET_DELAY": "0",
        }
    )
    # The default config file is a system path that will not exist in a
    # checkout. Naming one that certainly does not exist keeps a real
    # /etc/readerboard/config.toml on a development machine from quietly
    # overriding any of the above.
    env["READERBOARD_CONFIG_FILE"] = str(args.state_path.with_suffix(".no-config.toml"))

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
    """Start the client, pointed at the service that is already up."""
    # After the service rather than before it. The client asks an address for
    # its /openapi.json the first time that address answers anything, and
    # starting it last keeps a connection refused from being the first thing it
    # ever sees on this one.
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
    """Work out the host to print in the ready line."""
    configured = os.environ.get("READERBOARD_HOST", "")
    if configured in ("", "0.0.0.0", "::"):
        return "127.0.0.1"
    return configured


def _discard_state(path: Path) -> None:
    """Remove the state file so the service reconfigures the empty simulator."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as err:
        print("[run] could not remove %s: %s" % (path, err), file=sys.stderr)
        return
    print("[run] discarded %s, so the sign is configured from scratch" % path.name)


if __name__ == "__main__":
    raise SystemExit(main())
