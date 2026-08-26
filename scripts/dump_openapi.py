#!/usr/bin/env python3
"""Write the API's OpenAPI description to a file.

FastAPI can produce this without running anything, so no server is started, no
sign is touched and no port is listened on. The previous version of this project
had to boot a Flask app and fetch the schema over HTTP to get the same result.

    python scripts/dump_openapi.py docs/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from readerboard.api.app import create_app
from readerboard.config import Settings


def main() -> int:
    """Write the description to the path given, or to stdout if none is given."""
    schema = create_app(Settings(serial_url="loop://")).openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if len(sys.argv) < 2:
        sys.stdout.write(text)
        return 0

    destination = Path(sys.argv[1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    print("wrote %s (%d paths)" % (destination, len(schema.get("paths", {}))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
