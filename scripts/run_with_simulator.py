#!/usr/bin/env python3
"""Run the service and the sign simulator together, already talking to each other.

Two processes, one command. The simulator is started first and its listening
address is read back from its own output, and the service is then started with
``READERBOARD_SERIAL_URL`` pointing at it. Both sets of output are prefixed and
streamed here, and Ctrl+C stops both.

    python scripts/run_with_simulator.py

The service ends up on http://127.0.0.1:5001 with its documentation at /docs,
and every transmission it makes appears decoded in the simulator window.

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
import contextlib
import importlib.util
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SIMULATOR = _ROOT / "tools" / "signsim" / "run.py"

DEFAULT_SIM_PORT = 4001
DEFAULT_API_PORT = 5001
DEFAULT_API_KEY = "local-development-key"
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
            "Ctrl+C stops both. Anything already set in the environment is passed "
            "through, so READERBOARD_SLOT_COUNT and the like still work."
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
        default=DEFAULT_API_KEY,
        help="the key every write needs. Without one the service refuses them all "
        "with a 503, so this has a development default rather than being required",
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
    """Start both, stream both, stop both."""
    args = build_parser().parse_args(argv)

    if importlib.util.find_spec("PySide6") is None:
        print(
            "PySide6 is not installed in %s, so the simulator cannot start.\n"
            "Install it with:\n"
            "    pip install --require-hashes -r tools/signsim/requirements.lock"
            % sys.executable,
            file=sys.stderr,
        )
        return 1

    if not args.keep_state:
        _discard_state(args.state_path)

    simulator = _start_simulator(args.sim_port)
    if simulator is None:
        return 1

    process, address = simulator
    print("[run] simulator listening on %s" % address)

    service = _start_service(args, address)
    print("[run] service on http://%s:%d, documentation at /docs"
          % (_api_host(), args.api_port))
    print("[run] Ctrl+C stops both")

    streams = [
        _stream(process.stdout, "sim"),
        _stream(service.stdout, "api"),
    ]
    return _wait(process, service, streams)


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
        env=_child_env(),
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
    env = _child_env()
    env.update(
        {
            "READERBOARD_SERIAL_URL": "socket://%s" % address,
            "READERBOARD_API_KEY": args.api_key,
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


def _child_env() -> dict[str, str]:
    """Build the environment both children start from."""
    env = dict(os.environ)
    # Without this a child's output sits in its buffer and the two streams
    # interleave in an order that has nothing to do with what happened.
    env["PYTHONUNBUFFERED"] = "1"
    return env


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


# ===========================================================================
# Running until something stops.
# ===========================================================================


def _stream(pipe: object, tag: str) -> threading.Thread:
    """Print one child's output, prefixed, on a thread of its own."""

    def pump() -> None:
        assert hasattr(pipe, "__iter__")
        for line in pipe:  # type: ignore[attr-defined]
            print("[%s] %s" % (tag, line.rstrip()))

    thread = threading.Thread(target=pump, name="stream-%s" % tag, daemon=True)
    thread.start()
    return thread


def _wait(
    simulator: subprocess.Popen[str],
    service: subprocess.Popen[str],
    streams: list[threading.Thread],
) -> int:
    """Wait for either child to finish, then stop the other."""
    children = {"sim": simulator, "api": service}
    try:
        while True:
            for tag, child in children.items():
                if child.poll() is not None:
                    print("[run] the %s exited with status %d, stopping the other"
                          % (tag, child.returncode))
                    _stop(other(children, tag))
                    return child.returncode
            # Waiting on one child at a time is enough: whichever it is, the
            # loop comes back around and notices the other.
            with contextlib.suppress(subprocess.TimeoutExpired):
                simulator.wait(timeout=0.3)
    except KeyboardInterrupt:
        # Ctrl+C in a console reaches the children too, so they are usually
        # already on their way out. Give them that chance before insisting.
        print("\n[run] stopping")
        for child in children.values():
            _stop(child)
        return 0
    finally:
        for thread in streams:
            thread.join(timeout=1.0)


def other(children: dict[str, subprocess.Popen[str]], tag: str) -> subprocess.Popen[str]:
    """Return the child that is not the one named."""
    return next(child for name, child in children.items() if name != tag)


def _stop(child: subprocess.Popen[str]) -> None:
    """Let a child finish, then make it."""
    if child.poll() is not None:
        return
    with contextlib.suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5.0)
        return

    child.terminate()
    try:
        child.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        child.kill()


if __name__ == "__main__":
    raise SystemExit(main())
