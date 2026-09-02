"""The names this tool answers to, so that they cannot drift apart.

They had. ``prog=`` said one thing, the Qt application name said another and the
window title named the sign's brand rather than this project, so the title bar
and the taskbar entry disagreed with each other while both disagreed with the
directory. Reading all three off one module is what stops that happening again.
``CHANGELOG.md`` records the three strings as they were.

Imports nothing, Qt included, so the test that pins it runs where PySide6 is
not installed. ``AGENTS.md`` carries the convention and the table for all three
components, and ``tests/test_component_names.py`` pins this module against it.
"""

from __future__ import annotations

__all__ = ["DISPLAY_NAME", "IDENTIFIER", "ORGANISATION", "PROSE_NAME"]

# Also the QSettings application name if this tool ever saves anything, which
# is why it stays a bare identifier rather than becoming the display name.
IDENTIFIER = "signsim"
DISPLAY_NAME = "readerboard sign simulator"
PROSE_NAME = "the sign simulator"

# The other half of that QSettings location, and the front half of the name
# this process gives the Windows taskbar. The service's identifier rather than
# this tool's, the same as the client.
ORGANISATION = "readerboard"
