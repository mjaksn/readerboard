"""Logging configuration.

Output goes to stderr, which is what journald reads when the service runs under
systemd, with an optional rotating file as well for anyone not running it that
way.

Log messages are documentation. They are the only account of what the service
did that anyone will read at three in the morning, so they say what happened and
what it means rather than dumping state. The one thing they never contain is the
API key.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_FILE_BYTES = 1_000_000
BACKUP_COUNT = 3


def configure(level: str = "INFO", log_file: Path | None = None) -> None:
    """Set up logging for the service. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(FORMAT, datefmt=DATE_FORMAT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=MAX_FILE_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    # uvicorn installs its own handlers; letting them propagate to ours would
    # print every access line twice.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).propagate = False
