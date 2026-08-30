"""Put the simulator and the service on the import path.

The simulator is not installed. It is a tool that lives in the checkout beside
the thing it listens to, and the service is imported from the same tree rather
than from site-packages, so that the tables it decodes against are the ones in
the working copy rather than whatever was last released.
"""

import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TOOL_ROOT.parent.parent

for _path in (str(_TOOL_ROOT), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)
