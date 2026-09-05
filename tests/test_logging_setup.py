"""What the log actually carries once the service has configured logging.

Why this file exists
====================

The service starts uvicorn with ``log_config=None``, so uvicorn installs no
handlers of its own and everything it has to say arrives by propagation to the
root logger. That is a quiet arrangement: setting ``propagate = False`` on the
uvicorn loggers, which is the obvious way to stop a doubled access line under
uvicorn's default configuration, instead leaves those records with no handler
at either end and the service logs no HTTP request at all. Nothing else fails.
The service starts, serves, drives the sign, and simply says nothing about the
requests it answered, which is not the sort of thing anybody reports.

So the two halves are pinned here: an access line reaches the handlers, and it
reaches them once.
"""

from __future__ import annotations

import logging

from readerboard import logging_setup

# The loggers uvicorn writes to. "uvicorn.access" is the one that carries a
# line per HTTP request; "uvicorn.error" carries the startup banner and the
# address actually bound, despite the name.
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class Capture(logging.Handler):
    """A handler that keeps what it was given."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _configured_capture(level: str = "INFO") -> Capture:
    """Configure logging, then watch the root handler list from the inside.

    ``configure`` clears the root handlers and installs its own, so a capture
    added beforehand would be thrown away. Patching the stream handler's
    formatter target is fussier than simply configuring first and adding the
    capture after, which is what a record propagating to the root will find.
    """
    logging_setup.configure(level)
    capture = Capture()
    logging.getLogger().addHandler(capture)
    return capture


def test_a_uvicorn_access_line_reaches_the_handlers():
    """An HTTP request line has somewhere to land."""
    capture = _configured_capture()
    try:
        logging.getLogger("uvicorn.access").info('GET /health HTTP/1.1" 200')
    finally:
        logging.getLogger().removeHandler(capture)

    assert [record.getMessage() for record in capture.records] == [
        'GET /health HTTP/1.1" 200'
    ]


def test_a_uvicorn_line_lands_exactly_once():
    """And it lands once, which is the other way this can go wrong."""
    capture = _configured_capture()
    try:
        for name in UVICORN_LOGGERS:
            logging.getLogger(name).info("one line from %s", name)
    finally:
        logging.getLogger().removeHandler(capture)

    assert len(capture.records) == len(UVICORN_LOGGERS)


def test_uvicorn_loggers_carry_no_handlers_of_their_own():
    """Nothing here installs handlers on them, so propagation is the only path.

    A handler added to one of these would be the doubling this arrangement is
    accused of, and it would come from somewhere other than ``configure``.
    """
    logging_setup.configure("INFO")
    for name in UVICORN_LOGGERS:
        logger = logging.getLogger(name)
        assert logger.handlers == []
        assert logger.propagate is True
