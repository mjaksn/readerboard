"""Run the service.

This is what the systemd unit invokes and what ``readerboard`` on the command
line runs. It exists so the unit does not have to know a uvicorn invocation, and
so the host and port come from the same configuration as everything else.

It is also the only place that knows the address actually being listened on,
which is why ``open_docs`` is handled here rather than in the application. The
application is mounted by uvicorn and is never told which port carried it.
"""

from __future__ import annotations

import argparse
import logging
import socket
import threading
import time
import webbrowser

import uvicorn

from readerboard import __version__, logging_setup, names
from readerboard.config import Settings

_log = logging.getLogger(__name__)

# How long to keep waiting for the port to answer before giving up on opening a
# browser. Startup is a second or two: the link to the sign is opened first, and
# a serial port that is not there takes a moment to say so. Well past that, and
# a service that has not bound by now has a worse problem than a missing tab.
OPEN_DOCS_TIMEOUT_SECONDS = 30.0

# How often to try the port while waiting.
OPEN_DOCS_POLL_SECONDS = 0.2


def main() -> int:
    """Start the HTTP server."""
    parser = argparse.ArgumentParser(
        prog=names.IDENTIFIER,
        description=(
            "Serve the readerboard API, which drives a BetaBrite Classic sign. "
            "Settings come from the config file "
            "(/etc/readerboard/config.toml unless READERBOARD_CONFIG_FILE says otherwise) "
            "and from environment variables prefixed READERBOARD_."
        ),
    )
    parser.add_argument("--version", action="version", version="%s %s" % (names.IDENTIFIER, __version__))
    parser.add_argument("--host", help="override the configured listen address")
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument(
        "--reload",
        action="store_true",
        help=(
            "restart when the source changes. For working on the service, not "
            "for running it: it watches the tree and costs a supervising process"
        ),
    )
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="show the settings in force, with the API key redacted, and exit",
    )
    args = parser.parse_args()

    settings = Settings()
    logging_setup.configure(settings.log_level, settings.log_file)

    if args.print_config:
        for key, value in sorted(settings.redacted().items()):
            print("%-32s %s" % (key, value))
        return 0

    host = args.host or settings.host
    port = args.port or settings.port

    if settings.open_docs:
        # Started before the server rather than after, because uvicorn.run does
        # not return until the server stops. The thread is a daemon so that a
        # service which never binds still exits on Ctrl+C rather than waiting
        # out the timeout.
        threading.Thread(
            target=open_docs_when_listening,
            args=(host, port),
            name="open-docs",
            daemon=True,
        ).start()

    # Under --reload this call supervises a worker rather than serving itself,
    # and the open-docs thread above stays here in the supervisor. That is what
    # makes it fire once at the first bind rather than on every restart.
    uvicorn.run(
        "readerboard.api.app:app",
        host=host,
        port=port,
        log_config=None,
        reload=args.reload,
    )
    return 0


def open_docs_when_listening(host: str, port: int) -> None:
    """Wait for the port to answer, then show the documentation in a browser.

    Waiting on the port rather than sleeping a fixed time is what makes this
    reliable on a slow start: a browser opened before the server binds shows a
    connection error, and the person then reloads a page that was going to work
    anyway.

    Nothing here is allowed to take the service down with it. A browser that
    cannot be launched, a headless machine, a port that never opens: each of
    those is a log line and nothing more, because the service's job is to drive
    a sign and it does that whether or not anybody is looking at ``/docs``.
    """
    address = browsable_host(host)
    if not _wait_for_port(address, port):
        _log.warning(
            "open_docs is set but %s:%d did not answer within %.0f seconds; "
            "not opening a browser",
            address,
            port,
            OPEN_DOCS_TIMEOUT_SECONDS,
        )
        return

    url = "http://%s:%d/docs" % (address, port)
    try:
        opened = webbrowser.open(url)
    # Deliberately every exception: webbrowser reaches out to whatever the
    # desktop has registered, and a browser must never stop the service.
    except Exception as err:
        _log.warning("open_docs could not start a browser for %s: %s", url, err)
        return
    if opened:
        _log.info("documentation opened at %s", url)
    else:
        _log.warning(
            "open_docs found no browser to open %s with; it is there when wanted", url
        )


def browsable_host(host: str) -> str:
    """Turn a listen address into one a browser can be pointed at.

    ``0.0.0.0`` and ``::`` mean every interface, which is a fine thing to bind
    and not an address anything can connect to. The loopback is the interface a
    browser on this machine has, and it is covered by both.
    """
    if host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if host in ("::", "[::]"):
        return "[::1]"
    return host


def _wait_for_port(host: str, port: int) -> bool:
    """Whether the port accepted a connection before the timeout ran out."""
    deadline = time.monotonic() + OPEN_DOCS_TIMEOUT_SECONDS
    target = host.strip("[]")
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((target, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(OPEN_DOCS_POLL_SECONDS)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
