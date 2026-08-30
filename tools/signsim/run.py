#!/usr/bin/env python3
"""Run the sign simulator out of a checkout, with nothing installed.

    python tools/signsim/run.py
    python tools/signsim/run.py --host 0.0.0.0 --port 4001

The simulator is a tool that lives beside the service rather than a package that
ships with it, so neither of them is on the import path by default. This puts
both there and hands over to ``signsim.app``. ``scripts/protocol_spike.py`` does
the same thing for the same reason.

Importing the service from this tree rather than from site-packages is the point
of the second insertion: the tables the simulator decodes against are then the
ones in the working copy, so a protocol constant changed on a branch is
understood here without a reinstall.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TOOL_ROOT.parent.parent

for _path in (str(_TOOL_ROOT), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from signsim.app import main  # noqa: E402 (the path has to be set up first)

if __name__ == "__main__":
    raise SystemExit(main())
