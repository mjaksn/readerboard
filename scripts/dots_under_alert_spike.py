#!/usr/bin/env python3
"""Ask the sign what it does with a picture written while an alert is up.

On 2026-09-13 a slot with icons, covered by an alert, came back with its icons
gone after the alert was released, and stayed that way until the next periodic
refresh wrote them again. PR #76 fixed it by handing the sign back for the
write. What the fix did not settle is which of two things the sign was doing:

  A. it never took the write, so the file stayed as it was; or
  B. it took the write, stored the bytes, and would not draw them.

Nothing in the service can tell those apart, because the service only ever
writes. A SMALL DOTS PICTURE can be read back with ``J`` (4AH), and the Ethernet
adapter has been shown to answer reads, so this asks.

It also reproduces the message that first bit, rather than an invented one:

    <icon:rain><bold_on> <red><time> <icon:bolt>

which renders to a call to picture file ``6``, a character set, a space, red,
the time, a space, and a call to picture file ``8``. What was reported on the
display that day was **a literal 6 where the rain should have been and nothing
where the bolt should have been**, which is two different failures in one line
and is the other thing worth pinning: ``6`` is exactly the label byte of the
file the rain is called from, so it reads like the 14H being dropped and the
label falling through as text. The 2026-09-11 spike recorded that a call to a
missing picture draws nothing, which does not explain a visible ``6``.

WHAT IT SETTLES

 1. What ``J`` answers for a picture written while a priority message is
    running: the bytes that were sent, an empty file, or nothing at all.
 2. Whether that answer changes once the alert is released.
 3. What the display shows for the message above when its pictures were written
    under an alert, looked at in the two places separately.
 4. That the same message and the same pictures are right when they are written
    with the sign to itself, which is the control.
 5. Why an empty call prints its own label in one place and nothing in another,
    which the run of 2026-09-13 reproduced and could not explain. Steps 1b and
    1c put empty calls first, in the middle and last, and then one with a space
    after it, so position and "nothing follows it" can be told apart.

SAFETY

This is destructive. Step 1 writes a memory configuration, which erases every
message on the sign, so it refuses to run without --confirm-erase. Stop the
service first, and anything else that writes to the sign.

Afterwards the sign holds this script's memory configuration and not the
service's, and the service's state file cannot know that. Start the service and
reboot the sign through it (POST /sign/reboot, or Reboot in the client), which
writes the service's own configuration back and restores every slot from its
record.

    python scripts/dots_under_alert_spike.py --url socket://192.168.2.154:23 --confirm-erase
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from _sign_link import Link  # noqa: E402 (the path has to be set up first)

from readerboard import icons  # noqa: E402
from readerboard.protocol import constants as c  # noqa: E402
from readerboard.protocol import frames  # noqa: E402
from readerboard.protocol.markup import render  # noqa: E402
from readerboard.sign.controller import (  # noqa: E402
    MEMORY_CLEAR_SETTLE_SECONDS,
    RESET_SETTLE_SECONDS,
)
from readerboard.sign.layout import PICTURE_COLUMNS, PICTURE_ROWS  # noqa: E402

# The message that bit, and the two files its icons are called from. The labels
# are the service's own first two picture labels, so the rendered bytes are the
# ones the sign was actually sent that day.
MESSAGE = "<icon:rain><bold_on> <red><time> <icon:bolt>"
SLOT = b"A"
RAIN = b"6"
BOLT = b"8"

# Two more files, allocated and never written, for the question the message
# above raises and cannot answer: why one empty call printed its own label and
# the other printed nothing. Only the position in the message differs, so these
# put empty calls in every position at once.
SPARE_ONE = b"7"
SPARE_TWO = b"9"

# What the picture files hold, from the service's own library rather than
# invented here, so a wrong answer cannot be the bitmap's fault.
RAIN_ROWS = icons.resolve("rain", None)
BOLT_ROWS = icons.resolve("bolt", None)

observations: list[tuple[str, str]] = []

# Only used when there is nothing to read an answer from, so that a run piped
# somewhere still paces itself instead of racing through the display steps.
_FALLBACK_SECONDS = 15.0


def note(question: str, answer: str) -> None:
    """Record something for the summary at the end."""
    observations.append((question, answer))


def observe(what: str, question: str) -> str:
    """Hold the display still, say where to look, and wait for what was seen.

    The sign holds whatever was last written, so there is no hurry and no
    reason to guess how long somebody needs. Every earlier spike here waits on
    a person the same way, and two of the answers on record were wrong the
    first time because something brief was missed; a timer is what causes that.

    A run with nothing attached to type into falls back to a pause rather than
    failing, and records no answer, because an unanswered question must not
    come out of the summary looking answered.
    """
    print()
    print("  " + "-" * 74)
    print("  LOOK AT THE SIGN NOW.")
    for line in what.splitlines():
        print("  %s" % line)
    print("  " + "-" * 74)
    try:
        answer = input("  %s " % question).strip()
    except EOFError:
        print(
            "  (no keyboard attached to this run, so pausing %.0fs and asking nothing)"
            % _FALLBACK_SECONDS
        )
        time.sleep(_FALLBACK_SECONDS)
        answer = ""
    note(question, answer or "(not answered)")
    return answer


def dots_allocation(label: bytes) -> frames.FileAllocation:
    """Allocate a picture file exactly as the service does.

    Eight colour and one geometry for every file, which is what
    ``Layout`` builds, rather than a file cut to each icon. Both matter. Three
    colour maps Table 22's pixel codes modulo 4, so the 8s these two icons are
    drawn in come back as 0 and the bitmap reads as blank through no fault of
    the sign; that was this spike's own mistake on its first run. And a uniform
    geometry is what the sign was holding when the bug bit.
    """
    return frames.FileAllocation.dots(label, PICTURE_ROWS, PICTURE_COLUMNS)


class Spike:
    """The link, and the pacing between writes."""

    def __init__(self, link: Link, settle: float) -> None:
        """Hold what every step needs."""
        self.link = link
        self.settle = settle

    def send(self, payload: bytes, what: str, *, settle: float | None = None) -> None:
        """Transmit one payload and give the sign time to act on it."""
        packet = frames.packet(payload)
        print("  -> %-46s %4d bytes" % (what, len(packet)))
        self.link.send(packet)
        time.sleep(self.settle if settle is None else settle)

    def picture(self, label: bytes, rows: list[str], what: str) -> None:
        """Write a picture in one transmission, the way the service does."""
        self.send(frames.write_dots_file(label, rows), what)

    def read_picture(self, label: bytes, rows: list[str], when: str) -> str:
        """Read a picture back and say whether it holds what was written.

        The reply echoes the write command, the label, the height and the width,
        then the rows. So the bytes that were sent are recognisable inside it
        without parsing the framing.
        """
        packet = frames.packet(c.COMMAND_READ_DOTS + label)
        print("  -> %-46s %4d bytes" % ("read picture %s (J)" % label.decode(), len(packet)))
        reply = self.link.request(packet)

        if not reply:
            verdict = "nothing came back"
        else:
            wanted = b"".join(row.encode("ascii") + c.CR for row in rows)
            head = b"%02X%02X" % (len(rows), max(len(row) for row in rows))
            if wanted in reply:
                verdict = "holds exactly what was written"
            elif head in reply:
                verdict = "answered, right size, DIFFERENT pixels"
            elif c.COMMAND_WRITE_DOTS + label in reply:
                verdict = "answered for the file, no bitmap in it"
            else:
                verdict = "answered something else"

        shown = reply.lstrip(b"\x00")
        print("     <- %d bytes: %s" % (len(reply), shown[:120] if shown else "(nothing)"))
        print("     == %s" % verdict)
        note("J on picture %s, %s" % (label.decode(), when), verdict)
        return verdict


def run(spike: Spike) -> None:
    """Work through it."""
    body = render(MESSAGE, icons={("rain", None): RAIN, ("bolt", None): BOLT})

    print("\nThe message under test: %s" % MESSAGE)
    print("  renders to %r" % body)
    print("  rain is called from picture file %s, bolt from %s" % (RAIN.decode(), BOLT.decode()))

    # == 1. a known state, with both pictures allocated and empty =============
    print("\nStep 1: allocate, which erases the sign")
    allocations = [
        frames.FileAllocation(SLOT, 256),
        dots_allocation(RAIN),
        dots_allocation(BOLT),
        dots_allocation(SPARE_ONE),
        dots_allocation(SPARE_TWO),
    ]
    # The bare clear first: this sign displays nothing from a configuration that
    # no E$ preceded. See "A clear must come first" in docs/protocol-notes.md.
    spike.send(frames.clear_memory(), "clear memory (E$)", settle=MEMORY_CLEAR_SETTLE_SECONDS)
    spike.send(
        frames.set_memory_config(allocations),
        "memory configuration",
        settle=RESET_SETTLE_SECONDS,
    )
    spike.send(frames.set_run_sequence([SLOT]), "run sequence %s" % SLOT.decode())
    spike.read_picture(RAIN, RAIN_ROWS, "allocated, never written")

    # == 1b. what an empty picture call draws, by position ====================
    print("\nStep 1b: what an empty call draws, with every picture file still empty")
    print("  The message that bit opens with a call and closes with one, and the")
    print("  two behaved differently. Position is the only thing that differed, so")
    print("  this puts empty calls first, in the middle and last, all in one line.")

    # 14H and the label, built here rather than rendered, because these call
    # files no icon is in and the renderer only knows icon names.
    first = c.DOTS_INSERT + RAIN
    middle = c.DOTS_INSERT + SPARE_ONE
    last = c.DOTS_INSERT + BOLT
    probe = first + b"L" + middle + b"M" + last
    spike.send(
        frames.write_text_file(SLOT, probe, mode=c.MODE_HOLD),
        "TEXT %s = [6]L[7]M[8], every file empty" % SLOT.decode(),
    )
    observe(
        "Read the line out character by character. The letters L and M are drawn\n"
        "text and will be there; what matters is what sits around them.\n"
        "  6 L 7 M 8   every empty call prints its label\n"
        "  6 L 7 M     every call but the last one\n"
        "  6 L M       only a call that opens the message\n"
        "  L M         none of them, which is what the notes claim",
        "What does the line read, character by character?",
    )

    print("\nStep 1c: an empty call with a space after it, at the end of the line")
    print("  Separates 'nothing follows it' from 'it is not the first thing'.")
    spike.send(
        frames.write_text_file(
            SLOT, b"N" + c.DOTS_INSERT + SPARE_TWO + b" ", mode=c.MODE_HOLD
        ),
        "TEXT %s = N[9] and a space" % SLOT.decode(),
    )
    observe(
        "  N 9   the call prints its label when anything follows it\n"
        "  N     it does not, so being first is what decides it",
        "What does the line read?",
    )

    # == 2. take the sign over ================================================
    print("\nStep 2: raise an alert, so a priority message is running")
    spike.send(
        frames.write_text_file(c.FILE_PRIORITY, b"ALERT HOLDING THE SIGN", mode=c.MODE_HOLD),
        "priority file = ALERT HOLDING THE SIGN",
    )
    observe(
        "The alert should be holding the display on its own.\nNothing else should be visible.",
        "Is the alert holding the display, with nothing else showing? [y/describe]",
    )

    # == 3. write the pictures and the message underneath it ==================
    print("\nStep 3: write both pictures and the message, with the alert still up")
    spike.picture(RAIN, RAIN_ROWS, "picture %s = rain" % RAIN.decode())
    spike.picture(BOLT, BOLT_ROWS, "picture %s = bolt" % BOLT.decode())
    spike.send(frames.write_text_file(SLOT, body, mode=c.MODE_HOLD), "TEXT %s = the message" % SLOT.decode())

    # == 4. the question this spike exists for ================================
    print("\nStep 4: read both pictures back WHILE THE ALERT IS STILL UP")
    print("  This is the measurement. If the sign answers with the bytes that")
    print("  were just sent, it stored them and would not draw them. If it")
    print("  answers empty, it never took the write at all.")
    spike.read_picture(RAIN, RAIN_ROWS, "written under an alert, alert still up")
    spike.read_picture(BOLT, BOLT_ROWS, "written under an alert, alert still up")

    # == 5. release, and look at the two positions separately =================
    print("\nStep 5: release the alert and look at the message")
    spike.send(frames.clear_priority_file(), "release the priority file")
    observe(
        "The message should read: [rain icon] [time] [bolt icon]\n"
        "Look at the TWO ICON POSITIONS SEPARATELY:\n"
        "  LEFT  , where the rain belongs: icon, blank, or a character?\n"
        "  RIGHT , where the bolt belongs: icon, blank, or a character?\n"
        "On 2026-09-13 this showed a literal 6 on the left and nothing on the right.",
        "Left position, then right position?",
    )

    # == 6. and read them again, now that nothing is holding the sign =========
    print("\nStep 6: read both pictures back again, with no alert up")
    spike.read_picture(RAIN, RAIN_ROWS, "written under an alert, alert released")
    spike.read_picture(BOLT, BOLT_ROWS, "written under an alert, alert released")

    # == 7. the control: the same writes with the sign to itself ==============
    print("\nStep 7: write both pictures again with nothing holding the sign")
    spike.picture(RAIN, RAIN_ROWS, "picture %s = rain, no alert" % RAIN.decode())
    spike.picture(BOLT, BOLT_ROWS, "picture %s = bolt, no alert" % BOLT.decode())
    spike.read_picture(RAIN, RAIN_ROWS, "written with no alert up")
    spike.read_picture(BOLT, BOLT_ROWS, "written with no alert up")
    observe(
        "The same message again, and this is the control.\n"
        "Both icons should be drawn properly now.\n"
        "If they are, the only difference from step 5 was the alert.",
        "Are both icons drawn properly? [y/describe]",
    )


def main() -> int:
    """Run the spike."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        required=True,
        help="pyserial URL for the sign, such as socket://192.168.2.154:23",
    )
    parser.add_argument("--baud", type=int, default=9600, help="line speed, default 9600")
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to wait after each write, default 2.0",
    )
    parser.add_argument(
        "--look",
        type=float,
        default=15.0,
        help="seconds to pause at each point worth watching when there is no "
        "keyboard attached. With one, each of those waits for you instead",
    )
    parser.add_argument(
        "--confirm-erase",
        action="store_true",
        help="required, because step 1 erases every message on the sign",
    )
    args = parser.parse_args()

    if not args.confirm_erase:
        parser.error(
            "this spike erases every message on the sign. Stop the service, and "
            "anything else that writes to it, then pass --confirm-erase."
        )

    global _FALLBACK_SECONDS
    _FALLBACK_SECONDS = args.look

    print("readerboard picture-under-an-alert spike")
    print("Sign: %s at %d baud" % (args.url, args.baud))
    print("It stops at each thing worth looking at and waits for you to answer.")

    link = Link(args.url, args.baud)
    spike = Spike(link, args.settle)
    try:
        run(spike)
    finally:
        # However this ended. An alert left holding the sign is the one thing a
        # crash here could strand, since step 2 writes to the priority file.
        released = link.send(frames.packet(frames.clear_priority_file()), attempts=6)
        link.close()
        print("\nPriority file released: %s" % ("yes" if released else "NO, RERUN THE RELEASE"))
        if link.reconnects:
            print("Reconnected %d time(s) during the run." % link.reconnects)

    print("\n" + "=" * 78)
    print("What the sign answered")
    print("=" * 78)
    for question, answer in observations:
        print("  %-56s %s" % (question, answer))
    print(
        "\nThe sign now holds this script's memory configuration, not the service's.\n"
        "Start the service and reboot the sign through it (POST /sign/reboot, or\n"
        "Reboot in the client), which writes the service's configuration back and\n"
        "restores every slot from its record."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
