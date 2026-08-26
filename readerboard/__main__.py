"""Run the service.

This is what the systemd unit invokes and what ``readerboard`` on the command
line runs. It exists so the unit does not have to know a uvicorn invocation, and
so the host and port come from the same configuration as everything else.
"""

from __future__ import annotations

import argparse

import uvicorn

from readerboard import __version__, logging_setup
from readerboard.config import Settings


def main() -> int:
    """Start the HTTP server."""
    parser = argparse.ArgumentParser(
        prog="readerboard",
        description=(
            "Serve the readerboard API, which drives a BetaBrite Classic sign. "
            "Settings come from the config file "
            "(/etc/readerboard/config.toml unless READERBOARD_CONFIG_FILE says otherwise) "
            "and from environment variables prefixed READERBOARD_."
        ),
    )
    parser.add_argument("--version", action="version", version="readerboard %s" % __version__)
    parser.add_argument("--host", help="override the configured listen address")
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument(
        "--print-config",
        action="store_true",
        help="show the settings in force, with the API key redacted, and exit",
    )
    args = parser.parse_args()

    settings = Settings()
    logging_setup.configure(settings.log_level, settings.log_file)

    if args.print_config:
        for key, value in sorted(settings.redacted().items()):
            print("%-32s %s" % (key, value))
        return 0

    uvicorn.run(
        "readerboard.api.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_config=None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
