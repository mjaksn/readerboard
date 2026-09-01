"""Put the client and the service on the import path.

The client is not installed. It is a tool that lives in the checkout beside the
service it calls, and the OpenAPI description the catalogue is checked against is
read from the same tree, so a route added on a branch is compared against the
catalogue on that branch rather than against whatever was last released.
"""

import sys
from pathlib import Path

_TOOL_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _TOOL_ROOT.parent.parent

for _path in (str(_TOOL_ROOT), str(_REPO_ROOT)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Nothing is exported. Both tools have a conftest.py, pytest imports each by
# its basename, and a test that does `from conftest import ...` gets whichever
# one reached sys.modules first. The tests work the repository root out for
# themselves instead.
