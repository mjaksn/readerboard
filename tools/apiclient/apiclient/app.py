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
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from apiclient import names
from apiclient.window import DEFAULT_BASE_URL, MainWindow

# Drawn as the icon.svg beside it and rendered by scripts/render_icons.py.
ICON = Path(__file__).with_name("icon.ico")


def take_own_taskbar_button() -> None:
    """Give this process a taskbar button of its own on Windows.

    Windows files a window on the taskbar under its process's application user
    model id, which defaults to the path of the executable. For a Python
    program that is ``python.exe``, so left alone this window is grouped with
    every other Python window on the machine under Python's own icon, and the
    icon set on the application reaches the title bar and nothing else. Naming
    the process here, before it has a window, is what lets the taskbar show
    the same icon as the title bar, and what gives the client and the simulator
    a button each when they are run together. Elsewhere there is nothing to do:
    the other platforms take the icon from the window.
    """
    if sys.platform == "win32":
        import ctypes

        app_id = "%s.%s" % (names.ORGANISATION, names.IDENTIFIER)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)


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

    take_own_taskbar_button()
    application = QApplication(sys.argv[:1])
    # QSettings needs both of these before it will write anywhere sensible, and
    # the base URL is the only thing this tool ever saves.
    application.setOrganizationName(names.ORGANISATION)
    application.setApplicationName(names.IDENTIFIER)
    # On the application rather than the window, so that it reaches the
    # enumeration, error and history dialogs as well.
    application.setWindowIcon(QIcon(str(ICON)))

    window = MainWindow()
    if args.base_url:
        window.base_url.setText(args.base_url)
    window.show()
    return int(application.exec())
