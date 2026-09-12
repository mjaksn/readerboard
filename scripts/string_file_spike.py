#!/usr/bin/env python3
"""Find out how this sign really handles STRING files, before the service relies on them.

A STRING file is a small buffered file that a TEXT file calls inline, with 10H
followed by the STRING's label. The document says writing one does not blank the
display, which is the whole attraction: a temperature or a count could change
without the message around it restarting. The service is about to build on that,
and on several other things the document says about STRING files, and the
document has been wrong about this hardware four times already. So this asks the
sign first.

WHAT IT SETTLES

 1. Whether lowercase labels are files of their own, apart from uppercase ones,
    which decides the labels the service hands out.
 2. Whether a STRING write really changes the value without a blank, in HOLD and
    in the middle of a ROTATE scroll.
 3. Whether fixed width spacing in the calling TEXT file stops a changing value
    from shifting the text around it, which Appendix D says it does.
 4. Whether the control codes Table 18 allows in a STRING work in one.
 5. Whether the ones it leaves out really fail: flash, the 1DH attributes, both
    forms of extended character, the rainbow colours, the date, a new page, and a
    STRING calling another STRING.
 6. What a call shows when it points at nothing: a STRING allocated and never
    written, a label never allocated, and a TEXT file.
 7. Whether the sign keeps a STRING to the size it was allocated, and to the 125
    bytes the document gives as the limit.
 8. Whether the priority file can call a STRING, and whether writing that STRING
    while an alert is up leaves the alert holding the sign.
 9. What the sign answers when a STRING is read, and whether the display pauses
    while it does.
10. Whether a STRING survives the sign being power cycled. This step asks first
    and can be skipped.

Every sample carries its own number, so a write the sign refused shows up as a
number that did not change rather than as something to guess about.

SAFETY

This is destructive. Step 1 writes a memory configuration, which erases every
message on the sign, so it refuses to run without --confirm-erase. Stop the
service first, and anything else that writes to the sign.

Afterwards the sign holds this script's memory configuration and not the
service's, and the service's state file cannot know that. Start the service and
reboot the sign through it (POST /sign/reboot, or Reboot in the client), which
writes the service's own configuration back and restores every slot from its
record.

    python scripts/string_file_spike.py --url socket://192.168.2.154:23 --confirm-erase

If step 1 shows lowercase labels colliding with uppercase ones, run it again with
labels that cannot collide:

    python scripts/string_file_spike.py --url ... --confirm-erase --string-labels 123459
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

# The TEXT file every step calls from, and a second one that step 6 calls as
# though it were a STRING.
TEMPLATE = b"A"
OTHER_TEXT = b"B"

# The document's limit for a STRING file: "Because the STRING file data is
# buffered, the size of a STRING file is limited to 125 bytes."
STRING_LIMIT = 125

VALUE_CAPACITY = 32
SMALL_CAPACITY = 16

# What the six labels passed in --string-labels are for, in order. The last one is
# deliberately left out of the memory configuration.
ROLES = ("value", "nested", "small", "unwritten", "large", "unallocated")

observations: list[tuple[str, str]] = []


def call(label: bytes) -> bytes:
    """Return the bytes a TEXT file uses to call the STRING file ``label``."""
    return c.STRING_FILE_INSERT + label


def write_string(label: bytes, data: bytes) -> bytes:
    """Build a Write STRING payload by hand, with no length check, so step 7 can overrun."""
    return c.COMMAND_WRITE_STRING + label + data


def read_string(label: bytes) -> bytes:
    """Build a Read STRING payload by hand."""
    return c.COMMAND_READ_STRING + label


def string_allocation(label: bytes, capacity: int) -> frames.FileAllocation:
    """Allocate a STRING file the way Table 15 requires: locked, with 0000 as its schedule."""
    return frames.FileAllocation(
        label, capacity, file_type=c.FILE_TYPE_STRING, locked=True, schedule=b"0000"
    )


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
    """The link to the sign, the labels in use, and the pacing between writes."""

    def __init__(self, link: Link, labels: dict[str, bytes], settle: float) -> None:
        """Hold what every step needs, with each label named by what it is for."""
        self.link = link
        self.labels = labels
        self.settle = settle
        self.value = labels["value"]
        self.nested = labels["nested"]
        self.small = labels["small"]
        self.unwritten = labels["unwritten"]
        self.large = labels["large"]
        self.unallocated = labels["unallocated"]

    def send(self, payload: bytes, what: str, *, settle: float | None = None) -> None:
        """Transmit one payload and give the sign time to act on it."""
        packet = frames.packet(payload)
        print("  -> %-44s %3d bytes  %s" % (what, len(packet), packet.hex()))
        self.link.send(packet)
        time.sleep(self.settle if settle is None else settle)

    def request(self, payload: bytes, what: str) -> bytes:
        """Send a read and print whatever the sign answers."""
        packet = frames.packet(payload)
        print("  -> %-44s %3d bytes  %s" % (what, len(packet), packet.hex()))
        reply = self.link.request(packet)
        if reply:
            print("     <- %d bytes: %r" % (len(reply), reply.lstrip(b"\x00")))
        else:
            print("     <- nothing")
        return reply

    def template(self, body: bytes, what: str, *, mode: bytes = c.MODE_HOLD) -> None:
        """Rewrite the TEXT file every step calls from."""
        self.send(frames.write_text_file(TEMPLATE, body, mode=mode), what)

    def string(self, label: bytes, data: bytes, what: str, *, settle: float | None = None) -> None:
        """Write a STRING file."""
        self.send(write_string(label, data), what, settle=settle)


def step_1_labels(spike: Spike) -> None:
    """Allocate TEXT and STRING files side by side and check the lowercase ones are separate."""
    print("\nStep 1: allocate the files (this erases the sign)")
    strings = ", ".join(spike.labels[role].decode() for role in ROLES[:-1])
    print(
        "  TEXT files %s and %s, and STRING files %s. Nothing is allocated as %s."
        % (TEMPLATE.decode(), OTHER_TEXT.decode(), strings, spike.unallocated.decode())
    )

    allocations = [
        frames.FileAllocation(TEMPLATE, 256),
        frames.FileAllocation(OTHER_TEXT, 64),
        string_allocation(spike.value, VALUE_CAPACITY),
        string_allocation(spike.nested, VALUE_CAPACITY),
        string_allocation(spike.small, SMALL_CAPACITY),
        string_allocation(spike.unwritten, VALUE_CAPACITY),
        string_allocation(spike.large, STRING_LIMIT),
    ]
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
    listed = [
        spike.labels[role].decode()
        for role in ROLES[:-1]
        if spike.labels[role] + c.FILE_TYPE_STRING in reply
    ]
    note("STRING files listed in the F$ reply", ", ".join(listed) or "none")
    for label in (TEMPLATE, OTHER_TEXT):
        if label + c.FILE_TYPE_TEXT not in reply:
            note("TEXT file %s in the F$ reply" % label.decode(), "missing")

    spike.string(spike.value, b"low", "STRING %s = low" % spike.value.decode())
    spike.template(b"UP:" + call(spike.value), "TEXT %s = UP: then a call" % TEMPLATE.decode())
    spike.send(frames.set_run_sequence([TEMPLATE]), "run sequence %s" % TEMPLATE.decode())
    ask("Does the sign read UP:low? If it reads only low, or only UP:, the labels collide. [y/describe]")


def step_2_no_blank(spike: Spike) -> None:
    """Compare a TEXT rewrite, which blinks, with a STRING write, which should not."""
    print("\nStep 2: does a STRING write change the value without a blank?")
    print("  First the reference: the whole TEXT file rewritten, which is what the")
    print("  service does today. Watch what each rewrite does to the display.")
    for number in (70, 71, 72):
        spike.template(b"T=%dF" % number, "TEXT %s = T=%dF" % (TEMPLATE.decode(), number))
    ask("Did each of those rewrites blink or restart the display? [y/n]")

    print("\n  Now the same number held in a STRING, changed six times.")
    spike.string(spike.value, b"73", "STRING %s = 73" % spike.value.decode())
    spike.template(b"T=" + call(spike.value) + b"F", "TEXT %s = T=, call, F" % TEMPLATE.decode())
    for number in range(74, 80):
        spike.string(spike.value, b"%d" % number, "STRING %s = %d" % (spike.value.decode(), number))
    ask("Did the number change without a blink this time? [y/n]")

    print("\n  Now in ROTATE, changing the value while the message is scrolling past.")
    spike.string(spike.value, b"60", "STRING %s = 60" % spike.value.decode())
    spike.template(
        b"Outside it is " + call(spike.value) + b" degrees and the wind is light",
        "TEXT %s scrolls, with a call" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    time.sleep(4)
    for number in range(61, 73):
        spike.string(
            spike.value, b"%d" % number, "STRING %s = %d" % (spike.value.decode(), number), settle=1.5
        )
    ask("Did the scroll restart, jump or blank on any update? [y/n]")
    ask("Did the number change while it was on screen, or only on the next pass? [describe]")


def step_3_jitter(spike: Spike) -> None:
    """See the jitter Appendix D warns about, and whether fixed width stops it."""
    print("\nStep 3: does a changing value push the text around it?")
    print("  Appendix D says proportional spacing and centering make the rest of the")
    print("  message move as the value's width changes, and that fixed width stops it.")
    print("  Watch END while the value alternates between 11 and 88.")

    spike.template(call(spike.value) + b" END", "TEXT %s = call, END" % TEMPLATE.decode())
    for data in (b"11", b"88") * 3:
        spike.string(spike.value, data, "STRING %s = %s" % (spike.value.decode(), data.decode()))
    ask("Proportional: did END move as the number changed? [y/n]")

    spike.template(
        c.FIXED_WIDTH_ON + call(spike.value) + b" END",
        "TEXT %s = fixed width, call, END" % TEMPLATE.decode(),
    )
    for data in (b"11", b"88") * 3:
        spike.string(spike.value, data, "STRING %s = %s" % (spike.value.decode(), data.decode()))
    ask("Fixed width: did END stay still this time? [y/n]")

    print("\n  The pattern the service will recommend: the degree sign in the TEXT file,")
    print("  after the call, since the document does not allow it inside a STRING.")
    spike.string(spike.value, b"72", "STRING %s = 72" % spike.value.decode())
    spike.template(
        call(spike.value) + c.XC_DEGREES + b"F",
        "TEXT %s = call, degree, F" % TEMPLATE.decode(),
    )
    ask("Does the sign read 72, a degree sign, then F? [y/describe]")


# Codes Table 18 allows in a STRING file. Each entry is a name, the data after the
# sample's number, what the sign should draw, an optional follow-up question, and
# the mode the calling TEXT file needs for the effect to be visible.
ALLOWED: list[tuple[str, bytes, str, str | None, bytes]] = [
    (
        "colour",
        c.TEXT_COLOR_RED + b"RED",
        "RED in red",
        "Is END red too, or back to the colour it had before the call?",
        c.MODE_HOLD,
    ),
    (
        "two colours",
        c.TEXT_COLOR_GREEN + b"OK" + c.TEXT_COLOR_AMBER + b"!",
        "OK in green, then ! in amber",
        None,
        c.MODE_HOLD,
    ),
    (
        "half height",
        c.CHARSET_5_NORMAL + b"SMALL",
        "SMALL in short characters",
        "Is END short too?",
        c.MODE_HOLD,
    ),
    ("proportional reference", b"iiii", "iiii, closely spaced", None, c.MODE_HOLD),
    ("fixed width", c.FIXED_WIDTH_ON + b"iiii", "iiii, widely spaced", None, c.MODE_HOLD),
    ("time", b"T" + c.CURTIME_INSERT, "T and the sign's clock", None, c.MODE_HOLD),
    ("new line", b"ONE" + c.CR + b"TWO", "ONE and TWO, somehow separated", None, c.MODE_HOLD),
    (
        "fast",
        c.SPEED_5 + b"fast fast fast fast fast",
        "a quick scroll",
        None,
        c.MODE_ROTATE,
    ),
    (
        "slow",
        c.SPEED_1 + b"slow slow slow slow slow",
        "a clearly slower scroll than the last",
        "Did END scroll slowly as well?",
        c.MODE_ROTATE,
    ),
]

# Codes Table 18 leaves out. The document's list is what the service's value
# whitelist will be built from, so each of these is expected to fail, and any
# that works is a candidate for widening it.
EXCLUDED: list[tuple[str, bytes, str, str | None, bytes]] = [
    ("rainbow", c.TEXT_COLOR_RAINBOW1 + b"RAINBOW", "RAINBOW as a rainbow", None, c.MODE_HOLD),
    ("colour mix", c.TEXT_COLOR_MIX + b"MIX", "MIX, each letter a different colour", None, c.MODE_HOLD),
    ("flash", c.CHAR_FLASH_ON + b"FLASH" + c.CHAR_FLASH_OFF, "FLASH, flashing", None, c.MODE_HOLD),
    (
        "bold, 1DH",
        c.CHAR_ATTRIB_WIDE_ON + b"BOLD" + c.CHAR_ATTRIB_WIDE_OFF,
        "BOLD in bold",
        None,
        c.MODE_HOLD,
    ),
    ("degree, 08H 49H", b"72" + c.XC_DEGREES, "72 and a degree sign", None, c.MODE_HOLD),
    ("degree, A9H", b"72" + c.DEGREES, "72 and a degree sign", None, c.MODE_HOLD),
    ("accent, 90H", b"CAF" + c.E_ACCENT, "CAF and an accented E", None, c.MODE_HOLD),
    ("day of week, 0BH", c.CURDATE_WEEKDAYY, "the day of the week", None, c.MODE_HOLD),
    ("new page, 0CH", b"ONE" + c.NEW_PAGE + b"TWO", "ONE then TWO as separate pages", None, c.MODE_HOLD),
]


def show_samples(
    spike: Spike, prefix: str, samples: list[tuple[str, bytes, str, str | None, bytes]]
) -> None:
    """Put each sample in the value STRING and ask what the sign drew."""
    print("  Each sample starts with its own number and the TEXT file adds END after it.")
    print("  Answer y if it drew what is described, r if the number did not change,")
    print("  which means the sign refused the write, or say what it drew instead.")
    mode: bytes | None = None
    for index, (name, data, expect, follow_up, sample_mode) in enumerate(samples, 1):
        number = "%s%d" % (prefix, index)
        if sample_mode != mode:
            spike.template(
                call(spike.value) + b" END",
                "TEXT %s = call, END" % TEMPLATE.decode(),
                mode=sample_mode,
            )
            mode = sample_mode
        spike.string(spike.value, number.encode() + b" " + data, "sample %s, %s" % (number, name))
        answer = input("\n  %s %s: expect %s. " % (number, name, expect)).strip()
        note("%s %s" % (number, name), answer or "(no answer)")
        if follow_up:
            ask("%s %s" % (number, follow_up))


def step_4_allowed(spike: Spike) -> None:
    """Try the control codes the document allows in a STRING."""
    print("\nStep 4: the control codes Table 18 allows in a STRING")
    show_samples(spike, "S", ALLOWED)


def step_5_excluded(spike: Spike) -> None:
    """Try what the document leaves out, including a STRING that calls another."""
    print("\nStep 5: what Table 18 leaves out, each expected to fail")
    spike.string(spike.nested, b"IN", "STRING %s = IN, for the nesting sample" % spike.nested.decode())
    samples = [
        *EXCLUDED,
        (
            "nested call",
            b"X" + call(spike.nested) + b"X",
            "XINX, a STRING calling another",
            None,
            c.MODE_HOLD,
        ),
    ]
    show_samples(spike, "X", samples)


def step_6_missing(spike: Spike) -> None:
    """See what a call shows when it points at nothing a STRING could hold."""
    print("\nStep 6: calls that point at nothing")
    spike.template(b"[" + call(spike.unwritten) + b"]", "TEXT %s = [call unwritten]" % TEMPLATE.decode())
    ask("Allocated, never written: what is between the brackets? [nothing/describe]")

    spike.template(
        b"[" + call(spike.unallocated) + b"]", "TEXT %s = [call unallocated]" % TEMPLATE.decode()
    )
    ask("Never allocated: what is between the brackets? [nothing/describe]")

    spike.send(frames.write_text_file(OTHER_TEXT, b"TXT"), "TEXT %s = TXT" % OTHER_TEXT.decode())
    spike.template(b"[" + call(OTHER_TEXT) + b"]", "TEXT %s = [call a TEXT file]" % TEMPLATE.decode())
    ask("A TEXT file called as a STRING: what is between the brackets? [nothing/describe]")


def step_7_capacity(spike: Spike) -> None:
    """Overrun a STRING's allocation, and then the document's 125 byte limit."""
    print("\nStep 7: what happens when a STRING is written past its size")
    print("  %s is allocated %d bytes." % (spike.small.decode(), SMALL_CAPACITY))
    spike.template(
        b"[" + call(spike.small) + b"]",
        "TEXT %s = [call small]" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    spike.string(spike.small, b"0123456789ABCDEF", "STRING %s = 16 bytes" % spike.small.decode())
    ask("Does it scroll [0123456789ABCDEF]? [y/describe]")
    spike.string(spike.small, b"0123456789ABCDEFGHIJKLMN", "STRING %s = 24 bytes" % spike.small.decode())
    ask("After 24 bytes: all of them to N, cut at F, or unchanged? [describe]")

    print("\n  %s is allocated %d bytes, the document's limit." % (spike.large.decode(), STRING_LIMIT))
    spike.template(
        b"[" + call(spike.large) + b"]",
        "TEXT %s = [call large]" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    spike.string(spike.large, b"PREVIOUS", "STRING %s = PREVIOUS" % spike.large.decode())
    ask("Does it scroll [PREVIOUS]? [y/describe]")
    over = b"." * (STRING_LIMIT - 5) + b"LIMIT" + b"OVER!"
    spike.string(spike.large, over, "STRING %s = %d bytes" % (spike.large.decode(), len(over)))
    ask("After %d bytes: ending LIMITOVER!], ending LIMIT], or still PREVIOUS? [describe]" % len(over))


def step_8_priority(spike: Spike) -> None:
    """Call a STRING from the priority file, then change it while the alert holds the sign."""
    print("\nStep 8: a STRING inside an alert")
    spike.string(spike.value, b"50", "STRING %s = 50" % spike.value.decode())
    spike.template(b"ROTATION", "TEXT %s = ROTATION" % TEMPLATE.decode())
    try:
        spike.send(
            frames.write_text_file(c.FILE_PRIORITY, c.TEXT_COLOR_RED + b"ALERT " + call(spike.value)),
            "priority file = ALERT, call",
        )
        ask("Has ALERT 50 taken over the sign? [y/describe]")
        for number in range(51, 56):
            spike.string(spike.value, b"%d" % number, "STRING %s = %d" % (spike.value.decode(), number))
        ask("Did the number in the alert change? [y/n]")
        ask("Is the alert still holding the sign, rather than ROTATION? [y/n]")
    finally:
        spike.send(frames.clear_priority_file(), "release priority")
    ask("Is ROTATION back? [y/n]")


def step_9_read(spike: Spike) -> None:
    """Read a STRING back and see whether the display pauses while the sign answers."""
    print("\nStep 9: reading a STRING back")
    print("  The document says the sign pauses or blanks while it answers. Watch the scroll.")
    spike.string(spike.value, b"READ ME", "STRING %s = READ ME" % spike.value.decode())
    spike.template(
        b"Scrolling past " + call(spike.value) + b" while the sign is asked a question",
        "TEXT %s scrolls, with a call" % TEMPLATE.decode(),
        mode=c.MODE_ROTATE,
    )
    time.sleep(4)
    reply = spike.request(read_string(spike.value), "read STRING %s" % spike.value.decode())
    echoed = c.COMMAND_WRITE_STRING + spike.value + b"READ ME"
    note("Read STRING reply", repr(reply.lstrip(b"\x00")) if reply else "nothing")
    note("Reply carries G, the label and the data", "yes" if echoed in reply else "no")
    ask("Did the scroll pause or blank when the read went out? [describe]")

    reply = spike.request(read_string(spike.unallocated), "read unallocated STRING")
    note("Read of an unallocated STRING", repr(reply.lstrip(b"\x00")) if reply else "nothing")


def step_10_power(spike: Spike) -> None:
    """Power cycle the sign and see whether its STRING files survive."""
    print("\nStep 10: does a STRING survive a power cycle?")
    spike.string(spike.value, b"42", "STRING %s = 42" % spike.value.decode())
    spike.template(b"PC:" + call(spike.value), "TEXT %s = PC:, call" % TEMPLATE.decode())
    answer = input(
        "\n  Power cycle the sign itself now, not the adapter. When it has finished\n"
        "  starting up, press Enter. Type s and Enter to skip this step. "
    ).strip()
    if answer.lower().startswith("s"):
        note("Power cycle", "skipped")
        return
    ask("What does the sign show now? PC:42, PC: alone, nothing, or something else? [describe]")
    spike.request(frames.read_memory_config(), "read memory configuration (F$)")


STEPS = {
    1: step_1_labels,
    2: step_2_no_blank,
    3: step_3_jitter,
    4: step_4_allowed,
    5: step_5_excluded,
    6: step_6_missing,
    7: step_7_capacity,
    8: step_8_priority,
    9: step_9_read,
    10: step_10_power,
}


def parse_labels(text: str) -> dict[str, bytes]:
    """Check the labels asked for and name each by the role it plays."""
    if len(text) != len(ROLES) or len(set(text)) != len(text):
        raise argparse.ArgumentTypeError(
            "give exactly %d different labels, one each for %s" % (len(ROLES), ", ".join(ROLES))
        )
    forbidden = {c.FILE_PRIORITY.decode(), "?", TEMPLATE.decode(), OTHER_TEXT.decode()}
    for char in text:
        if not 0x20 < ord(char) < 0x7F:
            raise argparse.ArgumentTypeError("%r is not a printable label" % char)
        if char in forbidden:
            raise argparse.ArgumentTypeError(
                "%r cannot be a STRING label here; 0 and ? never can, and %s and %s are the "
                "spike's TEXT files" % (char, TEMPLATE.decode(), OTHER_TEXT.decode())
            )
    return {role: char.encode("ascii") for role, char in zip(ROLES, text, strict=True)}


def parse_steps(text: str) -> list[int]:
    """Turn a comma separated list of step numbers into the steps to run, always with step 1."""
    try:
        wanted = {int(part) for part in text.split(",") if part.strip()}
    except ValueError:
        raise argparse.ArgumentTypeError("steps are numbers, such as 1,4,5") from None
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
        "--string-labels",
        type=parse_labels,
        default=parse_labels("abcdez"),
        help=(
            "six STRING labels, for the value, nested, small, unwritten, large and "
            "unallocated files in that order; default abcdez"
        ),
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

    print("readerboard STRING file spike")
    print("Sign: %s at %d baud" % (args.url, args.baud))

    link = Link(args.url, args.baud)
    spike = Spike(link, args.string_labels, args.settle)
    try:
        for number in args.steps:
            STEPS[number](spike)
    finally:
        # However this ended. A priority file left holding the sign is the one
        # thing a crash here could strand, since step 8 writes to it.
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
    print("\nPaste this into docs/protocol-notes.md, under STRING files.")
    print(
        "\nThe sign now holds this script's memory configuration, not the service's.\n"
        "Start the service and reboot the sign through it (POST /sign/reboot, or\n"
        "Reboot in the client), which writes the service's configuration back and\n"
        "restores every slot from its record."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
