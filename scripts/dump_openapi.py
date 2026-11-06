#!/usr/bin/env python3
"""Write the API's OpenAPI description to a file.

FastAPI can produce this without running anything, so no server is started, no
sign is touched and no port is listened on. CI regenerates it and fails if the
committed file has drifted from the routes.

    python scripts/dump_openapi.py docs/openapi.json

With no path, or with ``-``, it goes to stdout. ``-`` used to be taken as a file
name, and the 0.7.0 release committed a copy of the description under it.
``--help`` was taken as one too, and wrote the description to a file of that
name rather than saying how to use this, so the arguments are parsed now and
anything else beginning with a hyphen is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from readerboard.api.app import create_app
from readerboard.config import Settings


def build_parser() -> argparse.ArgumentParser:
    """Describe the command line: one optional path, and nothing else."""
    parser = argparse.ArgumentParser(
        prog="dump_openapi.py",
        description=(
            "Write the API's OpenAPI description, without starting a server or "
            "touching a sign."
        ),
    )
    parser.add_argument(
        "path",
        nargs="?",
        default="-",
        help="where to write it, such as docs/openapi.json. Omitted or - writes to stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Write the description to the path given, or to stdout for none or ``-``."""
    args = build_parser().parse_args(argv)
    schema = create_app(Settings(serial_url="loop://")).openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"

    if args.path == "-":
        sys.stdout.write(text)
        return 0

    destination = Path(args.path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")
    print("wrote %s (%d paths)" % (destination, len(schema.get("paths", {}))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
