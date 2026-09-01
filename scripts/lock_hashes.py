#!/usr/bin/env python3
"""Rewrite the lock files so every pin carries the hashes pip must verify.

The lock file names an exact version of each runtime dependency. That says what
to install but not what the bytes should be, so an installer following it takes
whatever the index hands over. Adding the hashes lets pip be run with
--require-hashes, which refuses anything it was not told to expect.

Every published file for a pinned version is listed, not only the one this
machine would choose. The lock is consumed on more than one platform: a Pi
installing through scripts/install.sh, the container image on amd64, arm64 and
arm/v7, and a Windows checkout for development. Each of those selects a
different wheel, and a hash list missing that wheel fails the install rather
than merely skipping the check.

The hashes come from the PyPI JSON API, which reports the digest the index
itself holds for each file. Nothing is downloaded and nothing is executed.

    python scripts/lock_hashes.py                 rewrite every lock file
    python scripts/lock_hashes.py --check         exit 1 if it would change

The version pins and the environment markers are left exactly as they are. To
move a version, edit the pin by hand as before and then run this.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Every lock file in the tree. requirements.lock is what the service runs on;
# requirements-build.lock is the one package needed to turn this source tree
# into a wheel, pinned for the same reason and checked the same way; and the two
# tools under tools/ have one each, which no installer of the service reads but
# which are pinned here so that one command covers all four.
LOCK_FILES = (
    _ROOT / "requirements.lock",
    _ROOT / "requirements-build.lock",
    _ROOT / "tools" / "signsim" / "requirements.lock",
    _ROOT / "tools" / "apiclient" / "requirements.lock",
)

# name==version, then an optional ; marker. Anything already carrying hashes is
# matched too, so this is safe to run over its own output.
PIN = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)(?P<marker>\s*;[^\\\n]*)?")

TIMEOUT = 30


def hashes_for(name: str, version: str) -> list[str]:
    """Return the sha256 of every file PyPI holds for this exact version."""
    url = "https://pypi.org/pypi/%s/%s/json" % (name, version)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as err:
        raise SystemExit("%s %s: the index returned %s" % (name, version, err.code)) from err

    files = payload.get("urls", [])
    if not files:
        raise SystemExit("%s %s: the index lists no files for it" % (name, version))

    digests = sorted({entry["digests"]["sha256"] for entry in files})
    return digests


def rewrite(text: str) -> str:
    """Return the lock file with a hash block under every pin."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()

        # Continuation lines belong to a pin this loop has already rewritten.
        if line.lstrip().startswith("--hash="):
            continue

        found = PIN.match(line)
        if found is None:
            out.append(line)
            continue

        name = found.group("name")
        version = found.group("version")
        marker = (found.group("marker") or "").rstrip()

        digests = hashes_for(name, version)
        print("%-20s %-12s %d file(s)" % (name, version, len(digests)), file=sys.stderr)

        out.append("%s==%s%s \\" % (name, version, marker))
        for index, digest in enumerate(digests):
            last = index == len(digests) - 1
            out.append("    --hash=sha256:%s%s" % (digest, "" if last else " \\"))

    return "\n".join(out) + "\n"


def main() -> int:
    """Rewrite the lock files, or report whether they are already current."""
    parser = argparse.ArgumentParser(
        prog="lock_hashes.py",
        description=(
            "Rewrite the lock files so every pinned version carries the sha256 of "
            "every file the index publishes for it, which is what pip --require-hashes "
            "checks against."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the file is not what this would produce",
    )
    args = parser.parse_args()

    stale = False
    for lock_file in LOCK_FILES:
        current = lock_file.read_text(encoding="utf-8")
        updated = rewrite(current)
        # More than one of these is called requirements.lock, so the bare
        # name would not say which one is stale.
        name = lock_file.relative_to(_ROOT).as_posix()

        if current == updated:
            print("%s is current" % name)
            continue

        if args.check:
            print("%s is stale; run scripts/lock_hashes.py" % name, file=sys.stderr)
            stale = True
            continue

        lock_file.write_text(updated, encoding="utf-8")
        print("wrote %s" % lock_file)

    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
