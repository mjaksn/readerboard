"""The documentation page opens itself when an editor asks for it.

Why this file exists
====================

``open_docs`` exists for one reason: a person starting the service from an
editor wants ``/docs`` in front of them. Everything about it is therefore
allowed to fail quietly except the part that matters, which is that it never
takes the service down. A sign keeps working whether or not anybody is looking
at a browser tab, and a missing tab must never become a service that did not
start.

So the tests below are mostly about failure: no browser on the machine, a
browser that raises, a port that never answers. Each one has to end in a log
line and a live service.
"""

from __future__ import annotations

import logging
import socket
import threading
from contextlib import closing

import pytest

from readerboard import __main__ as entry
from readerboard.config import Settings


@pytest.fixture
def listening_port():
    """Bind a real socket, leave it listening, and close it at the end.

    A real one rather than a fake: what is being tested is that the wait can
    tell a port that answers from one that does not, and a fake socket would
    only test the fake.
    """
    with closing(socket.socket()) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


# ===========================================================================
# The setting.
# ===========================================================================


def test_open_docs_is_off_unless_asked_for() -> None:
    # The installed service runs on a Pi with nobody at the machine. If this
    # default ever flips, a headless box starts trying to launch browsers.
    assert Settings(serial_url="loop://").open_docs is False


def test_open_docs_is_settable_from_the_environment(monkeypatch) -> None:
    # This is the whole mechanism the editors use, so it is worth pinning
    # rather than assuming pydantic keeps doing it.
    monkeypatch.setenv("READERBOARD_OPEN_DOCS", "1")
    assert Settings(serial_url="loop://").open_docs is True


# ===========================================================================
# Which address a browser is pointed at.
# ===========================================================================


@pytest.mark.parametrize(
    ("listen", "browsable"),
    [
        # Every interface is a fine thing to bind and not somewhere to connect.
        ("0.0.0.0", "127.0.0.1"),
        ("", "127.0.0.1"),
        ("::", "[::1]"),
        ("[::]", "[::1]"),
        # Anything specific is already an address, and is left alone.
        ("127.0.0.1", "127.0.0.1"),
        ("192.168.1.40", "192.168.1.40"),
    ],
)
def test_the_browser_is_pointed_at_something_it_can_reach(listen, browsable) -> None:
    assert entry.browsable_host(listen) == browsable


# ===========================================================================
# Opening it.
# ===========================================================================


def test_it_opens_the_documentation_once_the_port_answers(listening_port, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: opened.append(url) or True)

    entry.open_docs_when_listening("0.0.0.0", listening_port)

    assert opened == ["http://127.0.0.1:%d/docs" % listening_port]


def test_it_waits_for_the_port_rather_than_opening_straight_away(monkeypatch):
    """A browser opened before the server binds shows a connection error.

    The port here is bound part way through the wait, which is what a slow
    start looks like: the link to the sign is opened before the socket is.
    """
    opened: list[str] = []
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(entry, "OPEN_DOCS_POLL_SECONDS", 0.01)

    with closing(socket.socket()) as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        # Not listening yet, so nothing can connect.
        started = threading.Event()

        def listen_shortly() -> None:
            started.wait(timeout=5)
            server.listen(1)

        binder = threading.Thread(target=listen_shortly)
        binder.start()

        waiter = threading.Thread(
            target=entry.open_docs_when_listening, args=("127.0.0.1", port)
        )
        waiter.start()
        assert not opened, "opened before anything was listening"

        started.set()
        binder.join(timeout=5)
        waiter.join(timeout=10)

    assert opened == ["http://127.0.0.1:%d/docs" % port]


# ===========================================================================
# Every way it can fail.
# ===========================================================================


def test_a_port_that_never_answers_is_a_log_line_and_nothing_else(monkeypatch, caplog):
    opened: list[str] = []
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(entry, "OPEN_DOCS_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(entry, "OPEN_DOCS_POLL_SECONDS", 0.01)

    # Port 1 on the loopback: reserved, and nothing of ours is on it.
    with caplog.at_level(logging.WARNING):
        entry.open_docs_when_listening("127.0.0.1", 1)

    assert not opened
    assert "did not answer" in caplog.text


def test_a_browser_that_raises_does_not_reach_the_service(listening_port, monkeypatch, caplog):
    def explode(url: str) -> bool:
        raise OSError("no display and no browser")

    monkeypatch.setattr(entry.webbrowser, "open", explode)

    # The assertion is that this returns at all. Raising here would go up
    # through the thread and, were it ever called inline, take the service out
    # over a browser.
    with caplog.at_level(logging.WARNING):
        entry.open_docs_when_listening("127.0.0.1", listening_port)

    assert "could not start a browser" in caplog.text


def test_a_machine_with_no_browser_says_so(listening_port, monkeypatch, caplog):
    # webbrowser.open returns False rather than raising when it finds nothing
    # to open with, which is the headless case and is not an error.
    monkeypatch.setattr(entry.webbrowser, "open", lambda url: False)

    with caplog.at_level(logging.WARNING):
        entry.open_docs_when_listening("127.0.0.1", listening_port)

    assert "no browser" in caplog.text
