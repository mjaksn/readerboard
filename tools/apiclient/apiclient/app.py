"""Starting the client.

    python tools/apiclient/run.py
    python tools/apiclient/run.py --base-url http://192.168.2.40:8000

The base URL is remembered between runs. The API key is not, and there is no
option to pass one on the command line: a key on a command line is a key in the
shell history, and this tool is not worth that.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from apiclient import names
from apiclient.window import DEFAULT_BASE_URL, MainWindow


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Read the command line."""
    parser = argparse.ArgumentParser(
        prog=names.IDENTIFIER,
        description=(
            "A desktop client for exercising the readerboard service's HTTP surface. "
            "Every endpoint is callable, responses are shown as text rather than as "
            "JSON, and the vocabulary is whatever the service answered when you asked "
            "it rather than anything compiled in here."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "where the service is listening, such as %s. Remembered between runs, so "
            "this is only needed to change it." % DEFAULT_BASE_URL
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the client until its window closes."""
    args = parse_args(argv)

    application = QApplication(sys.argv[:1])
    # QSettings needs both of these before it will write anywhere sensible, and
    # the base URL is the only thing this tool ever saves.
    application.setOrganizationName(names.ORGANISATION)
    application.setApplicationName(names.IDENTIFIER)

    window = MainWindow()
    if args.base_url:
        window.base_url.setText(args.base_url)
    window.show()
    return int(application.exec())
