"""Starting several processes, streaming their output, and stopping them together.

Both launchers beside this file do the same three things: start two or three
processes, print everything they say with a tag saying which one said it, and
take the rest down when one that matters goes away. This holds that half, so
that the launchers hold only the part that differs, which is what each child is
given.

    scripts/run_with_simulator.py   the service and the sign simulator
    scripts/run_against_a_sign.py   the service and the client, against real hardware

``scripts/`` is not a package, so a launcher puts this directory on the import
path before importing this. ``tools/signsim/run.py`` and
``tools/apiclient/run.py`` do the same thing for the same reason.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
from typing import IO

# How long to keep trying a port before giving up on it. A service that has not
# bound by now has a worse problem than a late client, and against a real sign
# the wait is real: the link is opened before the socket is, and an adapter that
# is not there takes its own timeout to say so.
PORT_TIMEOUT_SECONDS = 30.0

# How often to try the port while waiting.
PORT_POLL_SECONDS = 0.2


def child_env() -> dict[str, str]:
    """Build the environment every child starts from."""
    env = dict(os.environ)
    # Without this a child's output sits in its buffer and the streams
    # interleave in an order that has nothing to do with what happened.
    env["PYTHONUNBUFFERED"] = "1"
    return env


def stream(pipe: IO[str] | None, tag: str) -> threading.Thread:
    """Print one child's output, prefixed, on a thread of its own.

    The pipe is optional only because that is what ``Popen.stdout`` is typed as.
    Every caller here asked for one, so a missing one is a bug rather than a
    child with nothing to say.
    """
    assert pipe is not None

    def pump() -> None:
        for line in pipe:
            print("[%s] %s" % (tag, line.rstrip()))

    thread = threading.Thread(target=pump, name="stream-%s" % tag, daemon=True)
    thread.start()
    return thread


def port_in_use(host: str, port: int) -> bool:
    """Whether something already holds the address the service is about to bind.

    Asked by binding it, which is the only question that matters and the only
    one without a race worth caring about here. ``SO_REUSEADDR`` is deliberately
    not set: on Windows it lets a second socket take a port another process is
    already listening on, which would answer False to the one thing being asked.
    """
    try:
        candidates = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
    except socket.gaierror:
        # Not an address this machine can resolve. Whatever is about to bind it
        # will say so with more authority than a guess here would.
        return False

    family, kind, protocol, _, address = candidates[0]
    with socket.socket(family, kind, protocol) as probe:
        try:
            probe.bind(address)
        except OSError:
            return True
    return False


def wait_for_port(
    host: str,
    port: int,
    child: subprocess.Popen[str] | None = None,
    timeout: float = PORT_TIMEOUT_SECONDS,
) -> bool:
    """Whether the port accepted a connection before the timeout ran out.

    ``child`` is the process expected to open it. Watching it as well is what
    keeps a service that died at startup from costing the whole timeout before
    anybody is told, which against a sign that is not answering is exactly the
    case that happens.
    """
    deadline = time.monotonic() + timeout
    target = host.strip("[]")
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            return False
        try:
            with socket.create_connection((target, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(PORT_POLL_SECONDS)
    return False


def wait(
    children: dict[str, subprocess.Popen[str]],
    streams: list[threading.Thread],
    fatal: frozenset[str],
) -> int:
    """Wait until something ends, then stop whatever is still running.

    Only a tag in ``fatal`` ends the run. The service and whatever it is writing
    to are no use without each other, so either going away stops both. The
    client is a window for poking the service with, and closing it breaks
    nothing, so that is reported and the rest keeps running.
    """
    try:
        while True:
            for tag, child in list(children.items()):
                if child.poll() is None:
                    continue
                if tag not in fatal:
                    # Closing the window is how this one is meant to end, so it
                    # is reported as an ordinary thing rather than as a status.
                    if child.returncode == 0:
                        print("[run] the %s was closed, leaving the rest running" % tag)
                    else:
                        print("[run] the %s exited with status %d, leaving the rest running"
                              % (tag, child.returncode))
                    del children[tag]
                    continue
                print("[run] the %s exited with status %d, stopping the rest"
                      % (tag, child.returncode))
                for name, running in children.items():
                    if name != tag:
                        stop(running)
                return child.returncode
            # Waiting on one child at a time is enough: whichever it is, the
            # loop comes back around and notices the others. There is always one
            # to wait on, because a fatal tag is never dropped.
            with contextlib.suppress(subprocess.TimeoutExpired):
                next(iter(children.values())).wait(timeout=0.3)
    except KeyboardInterrupt:
        # Ctrl+C in a console reaches the children too, so they are usually
        # already on their way out. Give them that chance before insisting.
        print("\n[run] stopping")
        for child in children.values():
            stop(child)
        return 0
    finally:
        for thread in streams:
            thread.join(timeout=1.0)


def stop(child: subprocess.Popen[str]) -> None:
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


def require_qt(what: str, locks: list[str]) -> bool:
    """Say what to install when PySide6 is missing, and whether it is."""
    if importlib.util.find_spec("PySide6") is not None:
        return True
    print(
        "PySide6 is not installed in %s, so %s.\nInstall it with:\n%s"
        % (
            sys.executable,
            what,
            "\n".join("    pip install --require-hashes -r %s" % lock for lock in locks),
        ),
        file=sys.stderr,
    )
    return False
