#!/usr/bin/env python3
"""Put the display modes this service does not offer on a real sign.

Seventeen of the protocol's standard modes and all nineteen of its special ones
are offered by the service. The rest are absent because the document says they
belong to other signs, and this asks the sign whether the document is right.

That is not an idle question here. The document has been wrong about this
hardware four times already: it promised double height and a wide character set
that draw as ordinary text, a programmable tone on a fixed-pitch buzzer, and
twenty-four pictographs the sign renders as question marks. It has also been
wrong the other way, listing NEWS FLASH and TRUMPET as Betabrite-only when they
work fine. So "the table says no" is a reason to look, not a conclusion.

WHAT IT SHOWS

Three reference samples first, HOLD, ROTATE and FLASH, so that the two answers
that are not "it works" can be recognised on sight:

  * a mode the sign does not know usually falls back to one of these, and
    without a reference beside it, a fallback looks exactly like a feature;
  * a mode the sign rejects outright leaves the previous message on screen, so
    every sample carries its own label and an unchanged label means the write
    was refused rather than honoured.

Then each candidate, labelled with its own code. By default that is the five the
document rules out for a Betabrite. Pass --include-undocumented to also sweep
the codes the document does not mention at all, between Table 66's last entry
and Table 67's first, where an undocumented Betabrite mode would be hiding.

SAFETY

Everything goes to the priority file, which by protocol suppresses every other
file while it holds a message, so no slot is written and nothing the service
believes about the sign changes. The release runs in a finally: without it a
crash mid-parade leaves the priority file holding the sign, and the service
cannot undo that, because its suppression cache already believes the file is
empty and its own release never reaches the wire.

    python scripts/mode_parade.py --url socket://192.168.2.154:23
"""

from __future__ import annotations

import argparse
import time

import serial

from readerboard.protocol import constants as c
from readerboard.protocol import frames

COLOURS = {
    "green": c.TEXT_COLOR_GREEN,
    "red": c.TEXT_COLOR_RED,
    "amber": c.TEXT_COLOR_AMBER,
}

# What every sample says. Short enough to fit the display, so that a mode which
# holds it still can be told from one that scrolls it.
SAMPLE = b"TEST"

# The three a mode is most likely to be mistaken for, shown first.
REFERENCES = [
    (c.MODE_HOLD, "HOLD", "the message sits still"),
    (c.MODE_ROTATE, "ROTATE", "it travels right to left"),
    (c.MODE_FLASH, "FLASH", "it sits still and blinks"),
]

# Documented, and ruled out for a Betabrite. The reason is the document's own.
DOCUMENTED = [
    (b"d", "64H", "reserved, so the document gives it no meaning at all"),
    (b"m", "6DH", 'SCROLL: "pushes the bottom line to the top line if 2-line sign"'),
    (b"u", "75H", "EXPLODE, marked Alpha 3.0 protocol"),
    (b"v", "76H", "CLOCK, marked Alpha 3.0 protocol"),
    (b"nC", "6EH 43H", 'CYCLE COLORS: "will only work on AlphaEclipse 3600 signs"'),
]

# Undocumented. Table 66 ends its run at "C" and Table 67 begins at "S", and
# Table 67 skips "T". Nothing says what, if anything, lives in the gaps.
UNDOCUMENTED = [
    (b"n" + bytes([code]), "6EH %02XH" % code, "not in either table")
    for code in [*range(ord("D"), ord("S")), ord("T")]
]


class Link:
    """A connection to the sign that reopens itself when the adapter drops it."""

    def __init__(self, url: str, baud: int) -> None:
        """Remember where the sign is. Nothing is opened until the first send."""
        self._url = url
        self._baud = baud
        self._port: serial.Serial | None = None
        self.reconnects = 0

    def send(self, packet: bytes, *, attempts: int = 4) -> bool:
        """Send one transmission, reopening the link as many times as it takes."""
        for attempt in range(attempts):
            try:
                if self._port is None:
                    self._port = serial.serial_for_url(self._url, baudrate=self._baud, timeout=2)
                self._port.write(packet)
                self._port.flush()
                return True
            except Exception as err:
                self.close()
                if attempt == attempts - 1:
                    print("     ! giving up on this write: %s" % err)
                    return False
                self.reconnects += 1
                print("     . adapter dropped the link, reconnecting")
                time.sleep(1.5)
        return False

    def close(self) -> None:
        """Close the link, ignoring a port that will not close cleanly."""
        try:
            if self._port is not None:
                self._port.close()
        except Exception:
            pass
        self._port = None


def main() -> None:
    """Run the parade, releasing the sign however it ends."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="socket://192.168.2.154:23")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--hold", type=float, default=10.0, help="seconds per sample")
    parser.add_argument("--colour", choices=sorted(COLOURS), default="green")
    parser.add_argument(
        "--include-undocumented",
        action="store_true",
        help="also sweep the mode codes neither table mentions",
    )
    args = parser.parse_args()

    colour = COLOURS[args.colour]
    link = Link(args.url, args.baud)

    def show(label: bytes, mode: bytes) -> None:
        body = colour + label + b" " + SAMPLE
        packet = frames.packet(frames.write_text_file(c.FILE_PRIORITY, body, mode=mode))
        link.send(packet)
        time.sleep(args.hold)

    candidates = list(DOCUMENTED)
    if args.include_undocumented:
        candidates += UNDOCUMENTED

    try:
        print("Watch the sign. Each sample reads its own label then %r." % SAMPLE.decode())
        print("%.0fs each, in %s.\n" % (args.hold, args.colour))
        print("Two answers are not 'it works', so read for them:")
        print("  the label does not change  -> the sign refused the write")
        print("  it matches a reference     -> the sign fell back, it has no such mode\n")

        print("REFERENCES, so a fallback can be recognised:")
        for mode, name, expected in REFERENCES:
            print("  %-8s %s" % (name, expected))
            show(name.encode("ascii"), mode)

        print("\n%d CANDIDATES:" % len(candidates))
        for mode, hex_code, why in candidates:
            name = mode.decode("latin-1")
            print("  %-3s %-9s %s" % (name, hex_code, why))
            show(name.encode("latin-1"), mode)
    finally:
        # Always, however this ended. A stranded priority file holds the sign and
        # the service cannot clear it: its suppression cache already believes the
        # file is empty, so its own release never goes out.
        released = link.send(frames.packet(frames.clear_priority_file()), attempts=6)
        link.close()
        print("\nPriority file released: %s" % ("yes" if released else "NO, RERUN THE RELEASE"))
        if link.reconnects:
            print("Reconnected %d time(s) during the run." % link.reconnects)


if __name__ == "__main__":
    main()
