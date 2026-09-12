#!/usr/bin/env python3
"""Find out whether this sign draws SMALL DOTS PICTURE files, and how.

A SMALL DOTS PICTURE is a bitmap stored as a file of its own, up to 31 by 255
pixels, which a TEXT file draws inline by calling it with 14H and the picture's
label. On a sign seven dots high it is the way to put a heart, an arrow or a
logo beside the text, now that the pictograph range has turned out to be absent
from this sign. The document describes it in section 6.4 and Table 22, and the
document has been wrong about this hardware often enough that nothing is built
on it before the sign has been asked.

WHAT IT SETTLES

 1. Whether the memory configuration accepts DOTS files at all, and how the sign
    lists them when the configuration is read back.
 2. Whether a picture draws, alone and in the middle of a line of text, still
    and scrolling.
 3. Which of the nine pixel codes Table 22 lists this sign draws, and what each
    looks like: off, red, green, amber, dim red, dim green, brown, orange and
    yellow. The document warns that "some signs do not support the full range of
    colors".
 4. Whether the colour status in the memory configuration (1000 monochrome, 2000
    3-colour, 4000 8-colour) changes what the same picture looks like.
 5. How wide the display is in dots, and whether a picture wider than it
    scrolls through in ROTATE.
 6. What a picture taller than seven dots does: the document's own example is
    fifteen high.
 7. What the sign does with a picture bigger or smaller than its allocation.
 8. Whether the 100 millisecond pause the document asks for after the width is
    needed. The service writes a transmission in one piece, so this decides
    whether it could send pictures as it sends everything else.
 9. Whether rewriting a picture blanks the display, which the document says it
    does, unlike a STRING.
10. What a call draws when it points at a picture never written, or a label
    never allocated.
11. Whether the priority file can call a picture, for an alert with an icon.
12. What the sign answers when a picture is read back, and whether the display
    pauses while it does.

Every picture is a different colour or shape from the one before, so a write
the sign refused shows up as the old picture still being there rather than as
something to guess about.

SAFETY

This is destructive. Step 1 writes a memory configuration, which erases every
message on the sign, so it refuses to run without --confirm-erase. Stop the
service first, and anything else that writes to the sign.

Afterwards the sign holds this script's memory configuration and not the
service's, and the service's state file cannot know that. Start the service and
reboot the sign through it (POST /sign/reboot, or Reboot in the client), which
writes the service's own configuration back and restores every slot from its
record.

    python scripts/dots_spike.py --url socket://192.168.2.154:23 --confirm-erase
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _sign_link import Link

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.sign.controller import MEMORY_CLEAR_SETTLE_SECONDS, RESET_SETTLE_SECONDS

# The TEXT file every step calls from.
TEMPLATE = b"A"

# The pictures, each in a file of its own, by what it is for. Digits, so that
# none of them can be mistaken for a TEXT file or for the service's STRING
# files, and none is 0, which is the priority file.
HEART = b"1"
PALETTE_3 = b"2"
PALETTE_8 = b"3"
PALETTE_MONO = b"4"
RULER = b"5"
ARROW = b"6"
SIZED = b"7"
UNWRITTEN = b"8"
UNALLOCATED = b"9"

# The colour status Table 15 gives for a DOTS file, in the four characters a
# TEXT file uses for its schedule: "1000 = monochrome, 2000 = 3-color, 4000 =
# 8-color".
MONOCHROME = b"1000"
THREE_COLOUR = b"2000"
EIGHT_COLOUR = b"4000"

# The pixel codes in a Row Bit Pattern, from Table 22.
PIXEL_NAMES = {
    "0": "off",
    "1": "red",
    "2": "green",
    "3": "amber",
    "4": "dim red",
    "5": "dim green",
    "6": "brown",
    "7": "orange",
    "8": "yellow",
}

# "Following the Width bytes, there should be at least a 100 millisecond delay
# (not to exceed the timeout period) before sending the Row Bit Pattern." Twice
# that, to be clear of it, and far short of the sign's timeout.
AFTER_WIDTH_PAUSE = 0.2

# A heart seven dots wide, in red. Every other picture is built from this or
# from a function below, so each row is exactly the picture's width.
HEART_ROWS = [
    "0110110",
    "1111111",
    "1111111",
    "0111110",
    "0011100",
    "0001000",
    "0000000",
]

# The document's own example, from "Write SMALL DOTS PICTURE file" on page 76:
# an arrow fifteen high and nine wide, in red with one green dot at its point.
ARROW_ROWS = [
    "000000000",
    "000000000",
    "000100000",
    "000110000",
    "000111000",
    "000111100",
    "111111110",
    "111111112",
    "111111110",
    "000111100",
    "000111000",
    "000110000",
    "000100000",
    "000000000",
    "000000000",
]

# How far the ruler runs: well past any display this sign could have, so that
# where it stops is the display's edge rather than the picture's.
RULER_WIDTH = 120

observations: list[tuple[str, str]] = []


def call(label: bytes) -> bytes:
    """Return the bytes a TEXT file uses to draw the picture ``label``."""
    return c.DOTS_INSERT + label


def dots_allocation(label: bytes, rows: int, columns: int, colour: bytes) -> frames.FileAllocation:
    """Allocate a DOTS file the way Table 15 describes one.

    Its size is not bytes: "the first two bytes = # pixel rows and the last two
    bytes = the # of pixel columns in the picture", which is the same four hex
    digits a byte count would use, so the rows go in the high byte. The colour
    status goes where a TEXT file's schedule would.
    """
    return frames.FileAllocation(
        label, rows << 8 | columns, file_type=c.FILE_TYPE_DOTS, schedule=colour
    )


def solid(width: int, pixel: str, rows: int = 7) -> list[str]:
    """Return a picture that is one colour all over."""
    return [pixel * width] * rows


def palette() -> list[str]:
    """Return eight bands, one for each colour after off, two dots wide with a gap between."""
    row = "".join(pixel * 2 + "0" for pixel in "12345678")
    return [row] * 7


def ruler() -> list[str]:
    """Return a line along the bottom with a tick every ten dots, green every fiftieth."""
    ticks = []
    for column in range(1, RULER_WIDTH + 1):
        if column % 50 == 0:
            ticks.append("2")
        elif column % 10 == 0:
            ticks.append("1")
        else:
            ticks.append("0")
    tick_row = "".join(ticks)
    return [tick_row] * 6 + ["3" * RULER_WIDTH]


def halves(width: int) -> list[str]:
    """Return a picture red on its left half and green on its right."""
    return ["1" * (width // 2) + "2" * (width - width // 2)] * 7


def note(question: str, answer: str) -> None:
    """Record something observed, for the summary at the end."""
    observations.append((question, answer))


def ask(question: str) -> str:
    """Put a question to the person watching the sign and record the answer."""
    print()
    answer = input("  %s " % question).strip()
    note(question, answer or "(no answer)")
    return answer


class Spike:
    """The link to the sign and the pacing between writes."""

    def __init__(self, link: Link, settle: float) -> None:
        """Hold what every step needs."""
        self.link = link
        self.settle = settle

    def send(self, payload: bytes, what: str, *, settle: float | None = None) -> None:
        """Transmit one payload and give the sign time to act on it."""
        packet = frames.packet(payload)
        print("  -> %-44s %4d bytes  %s" % (what, len(packet), _preview(packet)))
        self.link.send(packet)
        time.sleep(self.settle if settle is None else settle)

    def request(self, payload: bytes, what: str) -> bytes:
        """Send a read and print whatever the sign answers."""
        packet = frames.packet(payload)
        print("  -> %-44s %4d bytes  %s" % (what, len(packet), packet.hex()))
        reply = self.link.request(packet)
        if reply:
            print("     <- %d bytes: %r" % (len(reply), reply.lstrip(b"\x00")))
        else:
            print("     <- nothing")
        return reply

    def template(self, body: bytes, what: str, *, mode: bytes = c.MODE_HOLD) -> None:
        """Rewrite the TEXT file every step calls from."""
        self.send(frames.write_text_file(TEMPLATE, body, mode=mode), what)

    def picture(
        self,
        label: bytes,
        rows: list[str],
        what: str,
        *,
        pause: bool = True,
        settle: float | None = None,
    ) -> None:
        """Write a SMALL DOTS PICTURE, pausing after the width as Table 22 asks.

        The height and width sent are the picture's own. Step 7 relies on that
        to write a picture that does not match its allocation.
        """
        height = len(rows)
        width = max(len(row) for row in rows)
        head = c.COMMAND_WRITE_DOTS + label + b"%02X%02X" % (height, width)
        # Every row ends in a CR, the last one included, which Table 22 allows:
        # "The last <CR> is optional."
        body = b"".join(row.encode("ascii") + c.CR for row in rows)
        whole = frames.packet(head + body)
        opening = frames.packet(b"")[: -len(c.EOT)] + head
        rest = body + c.EOT
        assert opening + rest == whole

        print(
            "  -> %-44s %4d bytes  %dx%d%s"
            % (what, len(whole), height, width, ", paused after the width" if pause else ", in one piece")
        )
        if pause:
            self.link.send(opening)
            time.sleep(AFTER_WIDTH_PAUSE)
            self.link.send(rest)
        else:
            self.link.send(whole)
        time.sleep(self.settle if settle is None else settle)


def _preview(packet: bytes, limit: int = 48) -> str:
    """Show the start of a packet in hex, since a picture's can run to hundreds of bytes."""
    shown = packet[:limit].hex()
    return shown if len(packet) <= limit else shown + "..."


def step_1_allocate(spike: Spike) -> None:
    """Allocate a TEXT file and the pictures, and see how the sign lists them."""
    print("\nStep 1: allocate the files (this erases the sign)")
    ruler_rows = ruler()
    allocations = [
        frames.FileAllocation(TEMPLATE, 256),
        dots_allocation(HEART, 7, len(HEART_ROWS[0]), THREE_COLOUR),
        dots_allocation(PALETTE_3, 7, len(palette()[0]), THREE_COLOUR),
        dots_allocation(PALETTE_8, 7, len(palette()[0]), EIGHT_COLOUR),
        dots_allocation(PALETTE_MONO, 7, len(palette()[0]), MONOCHROME),
        dots_allocation(RULER, 7, len(ruler_rows[0]), THREE_COLOUR),
        dots_allocation(ARROW, len(ARROW_ROWS), len(ARROW_ROWS[0]), THREE_COLOUR),
        dots_allocation(SIZED, 7, 8, THREE_COLOUR),
        dots_allocation(UNWRITTEN, 7, 8, THREE_COLOUR),
    ]
    print(
        "  TEXT file %s, and DOTS files %s. Nothing is allocated as %s."
        % (
            TEMPLATE.decode(),
            ", ".join(entry.label.decode() for entry in allocations[1:]),
            UNALLOCATED.decode(),
        )
    )
    for entry in allocations:
        print("     %s" % entry.encode().decode("ascii"))

    # The clear first, because this sign displays nothing from a configuration
    # that no bare E$ preceded. See "A clear must come first" in
    # docs/protocol-notes.md.
    spike.send(frames.clear_memory(), "clear memory (E$)", settle=MEMORY_CLEAR_SETTLE_SECONDS)
    spike.send(
        frames.set_memory_config(allocations),
        "memory configuration",
        settle=RESET_SETTLE_SECONDS,
    )

    reply = spike.request(frames.read_memory_config(), "read memory configuration (F$)")
    for entry in allocations[1:]:
        start = reply.find(entry.label + c.FILE_TYPE_DOTS)
        listed = reply[start : start + 11].decode("latin-1") if start >= 0 else "missing"
        note("F$ entry for DOTS file %s (sent %s)" % (entry.label.decode(), entry.encode().decode()), listed)

    spike.template(b"READY", "TEXT %s = READY" % TEMPLATE.decode())
    spike.send(frames.set_run_sequence([TEMPLATE]), "run sequence %s" % TEMPLATE.decode())
    ask("Does the sign read READY? [y/describe]")


def step_2_draw(spike: Spike) -> None:
    """Draw a picture alone, then inside a line of text, still and scrolling."""
    print("\nStep 2: does a picture draw?")
    spike.picture(HEART, HEART_ROWS, "DOTS %s = red heart" % HEART.decode())
    spike.template(call(HEART), "TEXT %s = the heart alone" % TEMPLATE.decode())
    ask("Is there a red heart, and where: left, centre or right? [describe]")

    spike.template(
        b"I " + call(HEART) + b" NY", "TEXT %s = I, the heart, NY" % TEMPLATE.decode()
    )
    ask("Does it read I, the heart, NY, on one line with spaces either side? [y/describe]")

    spike.template(
        c.TEXT_COLOR_GREEN + b"GREEN TEXT " + call(HEART) + b" GREEN TEXT",
        "TEXT %s = green text around the heart" % TEMPLATE.decode(),
    )
    ask("Is the heart still red, or did it take the text's green? [red/green/describe]")

    spike.template(
        b"A heart " + call(HEART) + b" scrolling past with the words around it",
        "TEXT %s scrolls, with the heart" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    ask("Does the heart scroll with the text, whole and in place? [y/describe]")


def show_palette(spike: Spike, label: bytes, status: str) -> None:
    """Write the eight-band palette into ``label`` and show it alone."""
    spike.picture(label, palette(), "DOTS %s = palette, %s" % (label.decode(), status))
    spike.template(call(label), "TEXT %s = palette %s alone" % (TEMPLATE.decode(), label.decode()))


def step_3_colours(spike: Spike) -> None:
    """Draw one band of each colour Table 22 lists."""
    print("\nStep 3: which colours does a picture draw?")
    print("  Eight bands, left to right, each two dots wide with a gap after it:")
    print("  " + ", ".join(PIXEL_NAMES[pixel] for pixel in "12345678") + ".")
    show_palette(spike, PALETTE_3, "3-colour")
    ask("How many bands can you see? [number]")
    ask("Describe each band's colour, left to right, and say which look the same. [describe]")


def step_4_colour_status(spike: Spike) -> None:
    """Draw the same palette from files allocated as 8-colour and as monochrome."""
    print("\nStep 4: does the colour status in the memory configuration matter?")
    print("  The same eight bands, from a file allocated as 8-colour.")
    show_palette(spike, PALETTE_8, "8-colour")
    ask("Does it look different from the 3-colour one? [n/describe]")

    print("\n  And from a file allocated as monochrome.")
    show_palette(spike, PALETTE_MONO, "monochrome")
    ask("Does it look different: all one colour, fewer bands, nothing? [n/describe]")


def step_5_width(spike: Spike) -> None:
    """Measure the display's width with a ruler, then scroll it."""
    print("\nStep 5: how wide is the display, and does a wide picture scroll?")
    print("  An amber line along the bottom with a tick every ten dots. Every fiftieth")
    print("  tick is green, the rest red. The ruler is %d dots long." % RULER_WIDTH)
    spike.picture(RULER, ruler(), "DOTS %s = ruler" % RULER.decode())
    spike.template(call(RULER), "TEXT %s = the ruler alone" % TEMPLATE.decode())
    ask("In HOLD, how many ticks can you count, and is the fifth one green? [describe]")
    ask("Does the line reach both edges, or is it cut, centred or missing? [describe]")

    spike.template(
        call(RULER) + b" END", "TEXT %s = the ruler, END" % TEMPLATE.decode(), mode=c.MODE_ROTATE
    )
    ask(
        "In ROTATE, does all of the ruler scroll through, twelve ticks with the fifth and "
        "tenth green, then END? [y/describe]"
    )


def step_6_height(spike: Spike) -> None:
    """Draw the document's own example, which is fifteen dots high."""
    print("\nStep 6: a picture taller than the display")
    print("  The document's own example: an arrow fifteen high and nine wide, pointing")
    print("  right, in red with one green dot at its point, which is the eighth row.")
    spike.picture(ARROW, ARROW_ROWS, "DOTS %s = the document's arrow" % ARROW.decode())
    spike.template(call(ARROW), "TEXT %s = the arrow alone" % TEMPLATE.decode())
    ask("What is drawn: the top seven rows, the middle, all of it squeezed, or nothing? [describe]")
    ask("Is the green dot visible? [y/n]")


def step_7_size(spike: Spike) -> None:
    """Write pictures larger and smaller than their allocation."""
    print("\nStep 7: a picture that does not match its allocation")
    print("  %s is allocated 7 by 8. First a picture that fits: 8 wide, all amber." % SIZED.decode())
    spike.picture(SIZED, solid(8, "3"), "DOTS %s = 7x8 amber" % SIZED.decode())
    spike.template(b"[" + call(SIZED) + b"]", "TEXT %s = [picture]" % TEMPLATE.decode())
    ask("Is there an amber block between the brackets? [y/describe]")

    print("\n  Now 16 wide, red on the left half and green on the right.")
    spike.picture(SIZED, halves(16), "DOTS %s = 7x16 red and green" % SIZED.decode())
    ask("All sixteen, only the red half, still amber, or nothing? [describe]")

    print("\n  Now 4 wide, all green.")
    spike.picture(SIZED, solid(4, "2"), "DOTS %s = 7x4 green" % SIZED.decode())
    ask("A green block half the width, a green block padded to 8, still the last one, or nothing? [describe]")


def step_8_pause(spike: Spike) -> None:
    """Write a picture in one piece, without the pause after the width."""
    print("\nStep 8: is the pause after the width needed?")
    print(
        "  Every picture so far paused %.1fs after its width, as Table 22 asks. This one"
        % AFTER_WIDTH_PAUSE
    )
    print("  goes in one piece, the way the service sends everything. It turns the heart green.")
    spike.picture(HEART, HEART_ROWS, "DOTS %s = red heart, paused" % HEART.decode())
    spike.template(call(HEART), "TEXT %s = the heart alone" % TEMPLATE.decode())
    green = [row.replace("1", "2") for row in HEART_ROWS]
    spike.picture(HEART, green, "DOTS %s = green heart, one piece" % HEART.decode(), pause=False)
    ask("Did the heart turn green, stay red, go blank, or come out damaged? [describe]")


def step_9_blank(spike: Spike) -> None:
    """Rewrite a picture while it is on screen and see what the display does."""
    print("\nStep 9: does rewriting a picture blank the display?")
    print("  The document says it does. Watch the text either side while the heart is")
    print("  rewritten four times, red and green in turn.")
    spike.template(
        b"LEFT " + call(HEART) + b" RIGHT", "TEXT %s = LEFT, heart, RIGHT" % TEMPLATE.decode()
    )
    for index in range(4):
        pixel = "2" if index % 2 else "1"
        rows = [row.replace("1", pixel) for row in HEART_ROWS]
        spike.picture(
            HEART, rows, "DOTS %s = %s heart" % (HEART.decode(), PIXEL_NAMES[pixel]), settle=2.0
        )
    ask("Did LEFT and RIGHT blank on each rewrite, or did only the heart change? [describe]")

    print("\n  Now in ROTATE, rewriting the heart while the message scrolls.")
    spike.template(
        b"Scrolling LEFT " + call(HEART) + b" RIGHT while the heart changes colour",
        "TEXT %s scrolls, with the heart" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    time.sleep(4)
    for index in range(4):
        pixel = "1" if index % 2 else "2"
        rows = [row.replace("1", pixel) for row in HEART_ROWS]
        spike.picture(
            HEART, rows, "DOTS %s = %s heart" % (HEART.decode(), PIXEL_NAMES[pixel]), settle=2.0
        )
    ask("Did the scroll restart, jump or blank on a rewrite? [describe]")


def step_10_missing(spike: Spike) -> None:
    """See what a call draws when it points at nothing."""
    print("\nStep 10: calls that point at nothing")
    spike.template(b"[" + call(UNWRITTEN) + b"]", "TEXT %s = [call unwritten]" % TEMPLATE.decode())
    ask("Allocated, never written: what is between the brackets? [nothing/describe]")

    spike.template(
        b"[" + call(UNALLOCATED) + b"]", "TEXT %s = [call unallocated]" % TEMPLATE.decode()
    )
    ask("Never allocated: what is between the brackets? [nothing/describe]")


def step_11_priority(spike: Spike) -> None:
    """Call a picture from the priority file, as an alert with an icon would."""
    print("\nStep 11: a picture inside an alert")
    spike.picture(HEART, HEART_ROWS, "DOTS %s = red heart" % HEART.decode())
    spike.template(b"ROTATION", "TEXT %s = ROTATION" % TEMPLATE.decode())
    try:
        spike.send(
            frames.write_text_file(
                c.FILE_PRIORITY, c.TEXT_COLOR_AMBER + b"ALERT " + call(HEART) + b" ALERT"
            ),
            "priority file = ALERT, heart, ALERT",
        )
        ask("Has ALERT, a red heart, ALERT taken over the sign? [y/describe]")
    finally:
        spike.send(frames.clear_priority_file(), "release priority")
    ask("Is ROTATION back? [y/n]")


def step_12_read(spike: Spike) -> None:
    """Read a picture back and see whether the display pauses while the sign answers."""
    print("\nStep 12: reading a picture back")
    print("  The document says the sign pauses while it answers. Watch the scroll.")
    spike.picture(HEART, HEART_ROWS, "DOTS %s = red heart" % HEART.decode())
    spike.template(
        b"Scrolling past " + call(HEART) + b" while the sign is asked a question",
        "TEXT %s scrolls, with the heart" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    time.sleep(4)
    reply = spike.request(c.COMMAND_READ_DOTS + HEART, "read DOTS %s" % HEART.decode())
    # Table 24: the sign answers with the write command code, I, the label and
    # the picture data, and "If <LF>s are sent, they will not be sent back".
    echoed = c.COMMAND_WRITE_DOTS + HEART + b"%02X%02X" % (7, len(HEART_ROWS[0]))
    note("Read DOTS reply", repr(reply.lstrip(b"\x00")) if reply else "nothing")
    note("Reply carries I, the label, the height and the width", "yes" if echoed in reply else "no")
    ask("Did the scroll pause or blank when the read went out? [describe]")


STEPS = {
    1: step_1_allocate,
    2: step_2_draw,
    3: step_3_colours,
    4: step_4_colour_status,
    5: step_5_width,
    6: step_6_height,
    7: step_7_size,
    8: step_8_pause,
    9: step_9_blank,
    10: step_10_missing,
    11: step_11_priority,
    12: step_12_read,
}


def parse_steps(text: str) -> list[int]:
    """Turn a comma separated list of step numbers into the steps to run, always with step 1."""
    try:
        wanted = {int(part) for part in text.split(",") if part.strip()}
    except ValueError:
        raise argparse.ArgumentTypeError("steps are numbers, such as 1,3,5") from None
    unknown = wanted - set(STEPS)
    if unknown:
        raise argparse.ArgumentTypeError(
            "no such step: %s. There are %d." % (", ".join(map(str, sorted(unknown))), len(STEPS))
        )
    # Every other step writes into the files step 1 allocates.
    return sorted(wanted | {1})


def main() -> int:
    """Run the spike."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--url",
        required=True,
        help="pyserial URL for the sign, such as socket://192.168.2.154:23 or /dev/ttyUSB0",
    )
    parser.add_argument("--baud", type=int, default=9600, help="line speed, default 9600")
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to wait after each write, default 2.0",
    )
    parser.add_argument(
        "--steps",
        type=parse_steps,
        default=sorted(STEPS),
        help="run only these steps, comma separated; step 1 always runs, since the rest need it",
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

    print("readerboard DOTS picture spike")
    print("Sign: %s at %d baud" % (args.url, args.baud))

    link = Link(args.url, args.baud)
    spike = Spike(link, args.settle)
    try:
        for number in args.steps:
            STEPS[number](spike)
    finally:
        # However this ended. A priority file left holding the sign is the one
        # thing a crash here could strand, since step 11 writes to it.
        released = link.send(frames.packet(frames.clear_priority_file()), attempts=6)
        link.close()
        print("\nPriority file released: %s" % ("yes" if released else "NO, RERUN THE RELEASE"))
        if link.reconnects:
            print("Reconnected %d time(s) during the run." % link.reconnects)

    print("\n" + "=" * 78)
    print("What the sign did")
    print("=" * 78)
    for question, answer in observations:
        print("  %-62s %s" % (question, answer))
    print("\nPaste this into the conversation, or into docs/protocol-notes.md.")
    print(
        "\nThe sign now holds this script's memory configuration, not the service's.\n"
        "Start the service and reboot the sign through it (POST /sign/reboot, or\n"
        "Reboot in the client), which writes the service's configuration back and\n"
        "restores every slot from its record."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
