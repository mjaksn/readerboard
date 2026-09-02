#!/usr/bin/env python3
"""Run the client out of a checkout, with nothing installed.

    python tools/apiclient/run.py
    python tools/apiclient/run.py --base-url http://192.168.2.40:5001

The client is a tool that lives beside the service rather than a package that
ships with it, so neither of them is on the import path by default. This puts
both there and hands over to ``apiclient.app``. ``tools/signsim/run.py`` and
``scripts/protocol_spike.py`` do the same thing for the same reason.

The service is imported from this tree rather than from site-packages for the
same reason the simulator does it: nothing here reads the service's code today,
but the checkout is what the tool is meant to be pointed at, and an import that
silently resolved to a released version would be a surprise waiting to happen.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _TOOL_ROOT.parent.parent

for _path in (str(_TOOL_ROOT), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from apiclient.app import main  # noqa: E402 (the path has to be set up first)

if __name__ == "__main__":
    raise SystemExit(main())
