"""The names this component answers to, so that they cannot drift apart.

Three tiers, because one name per component turned out not to be enough. The
identifier is what code, paths and settings keys use, and it is the expensive
one to change. The display name is what a person reads on a window, in a
launcher or in a unit file. The prose name is what sentences call it, in the
README, in a CI step, in a log line or in ``--help``.

For the service the first two are the same word, which is exactly why the tiers
are named separately rather than inferred: the two tools under ``tools/`` have
an identifier that is not a display name, and reading all three components off
one convention is the point.

``AGENTS.md`` carries the convention and the table for all three components.
``tests/test_component_names.py`` pins this module against it.
"""

from __future__ import annotations

__all__ = ["DISPLAY_NAME", "IDENTIFIER", "PROSE_NAME"]

IDENTIFIER = "readerboard"
DISPLAY_NAME = "readerboard"
PROSE_NAME = "the service"
