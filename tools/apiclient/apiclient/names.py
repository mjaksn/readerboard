"""The names this tool answers to, so that they cannot drift apart.

The identifier here is load bearing beyond the import path. ``app.py`` hands it
to ``setApplicationName``, and :class:`QSettings` builds its storage location
out of the organisation and application names, so this string is where the
remembered base URL lives. Changing it silently orphans that value. The window
title is the display name and costs nothing to change, which is the whole
reason the two are separate constants rather than one.

Imports nothing, Qt included, so the test that pins it runs where PySide6 is
not installed. ``AGENTS.md`` carries the convention and the table for all three
components, and ``tests/test_component_names.py`` pins this module against it.
"""

from __future__ import annotations

__all__ = ["DISPLAY_NAME", "IDENTIFIER", "ORGANISATION", "PROSE_NAME"]

IDENTIFIER = "apiclient"
DISPLAY_NAME = "readerboard client"
PROSE_NAME = "the client"

# The other half of the QSettings storage location, and the service's
# identifier rather than this tool's.
ORGANISATION = "readerboard"
