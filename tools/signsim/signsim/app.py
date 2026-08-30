"""Start the simulator: parse the command line, bind the socket, show the window.

The defaults are chosen so that the common case needs no arguments at all. It
listens on 127.0.0.1:4001, which is the port an Ethernet to RS-232 adapter
usually answers on, and it prints the ``serial_url`` to paste into the service
before the window appears. Binding to the loopback address by default is
deliberate: this accepts and interprets anything sent to it, and there is no
reason for that to be reachable from the rest of the network unless somebody
asks for it with ``--host``.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from signsim.model import SignState
from signsim.server import SignEndpoint
from signsim.window import MainWindow

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4001


def build_parser() -> argparse.ArgumentParser:
    """Build the command line, which is small on purpose."""
    parser = argparse.ArgumentParser(
        prog="signsim",
        description=(
            "A stand-in for a BetaBrite Classic. Listens on a TCP port, decodes "
            "everything the readerboard service writes to it, and shows both what "
            "the bytes mean and what the sign would now be holding. Point the "
            "service's serial_url at this and no hardware is needed."
        ),
        epilog=(
            "Example: run this, then start the service with "
            "READERBOARD_SERIAL_URL=socket://127.0.0.1:4001"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=(
            "address to bind. The default, %s, is reachable only from this machine. "
            "Use 0.0.0.0 to accept connections from elsewhere, remembering that this "
            "interprets whatever it is sent" % DEFAULT_HOST
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port to bind (default: %d, which is what the adapter answers on)" % DEFAULT_PORT,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the simulator. Returns the exit code Qt hands back."""
    args = build_parser().parse_args(argv)

    app = QApplication(sys.argv[:1])
    app.setApplicationName("readerboard sign simulator")

    endpoint = SignEndpoint()
    try:
        endpoint.listen(args.host, args.port)
    except OSError as err:
        print("%s" % err, file=sys.stderr)
        return 1

    print("listening on %s" % endpoint.endpoint)
    print("point the service at it with:")
    print("    READERBOARD_SERIAL_URL=%s" % endpoint.serial_url)

    window = MainWindow(endpoint, SignState())
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
