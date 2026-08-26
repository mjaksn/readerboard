#!/usr/bin/env python3
"""Settle against real hardware the questions the protocol document cannot answer.

The wire formats this service uses are quoted from the Alpha Sign
Communications Protocol and are not in doubt. What the document cannot say is
how your particular BetaBrite Classic behaves at the end of an Ethernet to
RS-232 adapter. Four things are genuinely open, and this script settles them:

1. Is the rotation seamless on this sign, with no blanking between files?
2. Does rewriting only the run sequence disturb the display? A slot expiring
   does exactly that, every time.
3. Does a run sequence write cancel a running priority message? The document
   says a write to the run time or run day table does, and is silent about the
   run sequence. Until this is answered, the service assumes it might and holds
   run sequence writes back while an alert is up.
4. Does the sign answer read commands through the adapter? If it does,
   divergence can be detected by asking rather than by re-pushing on a timer.

It also measures how long the sign really needs between packets, which the old
service never did; it just slept two seconds.

This script is destructive. Step 2 writes a memory configuration, and that
erases every message on the sign. It therefore refuses to run without
--confirm-erase.

Run it with the sign in front of you. It pauses to ask what you saw, then prints
a summary to paste into docs/protocol-notes.md.

    python scripts/protocol_spike.py --url socket://192.168.2.51:4001 --confirm-erase

Nothing else may be talking to the sign while this runs. Stop the service and
disable the crontab line first.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from readerboard.protocol import constants as c
from readerboard.protocol import frames
from readerboard.protocol.markup import render

POOL = [b"A", b"B", b"C"]
SLOT_CAPACITY = 256

observations: list[tuple[str, str]] = []


def note(question: str, answer: str) -> None:
    """Record something observed, for the summary at the end."""
    observations.append((question, answer))


def ask(question: str) -> str:
    """Put a question to the person watching the sign and record the answer."""
    print()
    answer = input("  %s " % question).strip()
    note(question, answer or "(no answer)")
    return answer


def send(link: serial.Serial, payload: bytes, *, label: str, settle: float) -> None:
    """Transmit one payload."""
    packet = frames.packet(payload)
    print("  -> %-38s %3d bytes  %s" % (label, len(packet), packet.hex()))
    link.write(packet)
    link.flush()
    time.sleep(settle)


def read_back(link: serial.Serial, payload: bytes, *, label: str, wait: float = 2.0) -> bytes:
    """Send a read command and return whatever the sign says, possibly nothing."""
    link.reset_input_buffer()
    packet = frames.packet(payload)
    print("  -> %-38s %3d bytes  %s" % (label, len(packet), packet.hex()))
    link.write(packet)
    link.flush()

    time.sleep(wait)
    reply = link.read(link.in_waiting or 1)
    if reply:
        print("     <- %d bytes: %r" % (len(reply), reply))
    else:
        print("     <- nothing")
    return reply


def step_1_transport(url: str, baud: int, settle: float) -> serial.Serial:
    """Open the link and prove a single write reaches the sign."""
    print("\nStep 1: open the transport")
    print("  serial_for_url(%r)" % url)
    link = serial.serial_for_url(url, baudrate=baud, timeout=5)
    print("  opened: %s" % link)

    send(
        link,
        frames.write_text_file(c.FILE_PRIORITY, render("SPIKE")),
        label="priority hello",
        settle=settle,
    )
    ask("Does the sign show SPIKE? [y/n]")
    send(link, frames.clear_priority_file(), label="clear priority", settle=settle)
    return link


def step_2_memory(link: serial.Serial, settle: float) -> None:
    """Allocate the file pool, and confirm that doing so erases the sign."""
    print("\nStep 2: set the memory configuration (this erases the sign)")
    allocations = [frames.FileAllocation(label, SLOT_CAPACITY) for label in POOL]
    print("  claiming %d bytes of the memory pool" % frames.memory_claimed(allocations))
    send(link, frames.set_memory_config(allocations), label="allocate A, B, C", settle=settle)
    ask("Did the sign go blank, and did any old message disappear? [y/n]")


def step_3_rotation(link: serial.Serial, settle: float) -> None:
    """Confirm the sign rotates several files by itself, without blanking."""
    print("\nStep 3: write three files and let the sign rotate them itself")
    for label, text in zip(POOL, ("<red>ONE", "<green>TWO", "<amber>THREE"), strict=True):
        send(
            link,
            frames.write_text_file(label, render(text)),
            label="write file %s" % label.decode(),
            settle=settle,
        )

    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    print("\n  Watch the sign for about half a minute. Nothing more is being sent.")
    ask("Does it cycle ONE, TWO, THREE by itself? [y/n]")
    ask("Is the rotation seamless, with no blanking between messages? [y/n]")

    print("\n  Now only the run sequence changes, which is what a slot expiring does.")
    send(link, frames.set_run_sequence([b"A", b"C"]), label="run sequence A C", settle=settle)
    ask("Has TWO dropped out, leaving ONE and THREE? [y/n]")
    ask("Did changing only the run sequence blank or restart the display? [y/n]")

    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)


def step_4_priority(link: serial.Serial, settle: float) -> None:
    """Confirm takeover, release, and whether a run sequence write cancels an alert."""
    print("\nStep 4: priority takeover and release")
    send(
        link,
        frames.write_text_file(c.FILE_PRIORITY, render("<red>ALERT")),
        label="priority write",
        settle=settle,
    )
    ask("Has ALERT taken over the whole sign, with the rotation stopped? [y/n]")

    print("\n  The important one. The document says a write to the run time or run")
    print("  day table cancels a running priority message, and says nothing about")
    print("  the run sequence. If the next write drops ALERT, the service must keep")
    print("  deferring run sequence writes during an alert, which is what it does now.")
    send(link, frames.set_run_sequence([b"A", b"B"]), label="run sequence A B", settle=settle)
    ask("Is ALERT still on the sign after that run sequence write? [y/n]")

    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    send(link, frames.clear_priority_file(), label="release priority", settle=settle)
    ask("Has the rotation resumed on its own? [y/n]")


def step_5_reads(link: serial.Serial) -> None:
    """Find out whether the sign answers read commands through this adapter."""
    print("\nStep 5: can the sign be asked what it is holding?")
    print("  Two-way traffic over the Ethernet adapter has never been tried. If it")
    print("  works, divergence can be detected by asking rather than by re-pushing")
    print("  everything on a timer.")

    replies = {
        "memory configuration": read_back(
            link, frames.read_memory_config(), label="read memory config (F$)"
        ),
        "memory pool size": read_back(
            link, frames.read_memory_pool_size(), label="read pool size (F#)"
        ),
        "run sequence": read_back(
            link, frames.read_run_sequence(), label="read run sequence (F.)"
        ),
        "run time table": read_back(
            link, frames.read_run_time_table(), label="read run time table (F))"
        ),
    }

    answered = [name for name, reply in replies.items() if reply]
    if answered:
        note("Reads that came back", ", ".join(answered))
        print("\n  The sign answered %d of 4 reads." % len(answered))
    else:
        note("Reads that came back", "none")
        print("\n  The sign answered nothing. Reconciliation stays on the timer.")


def step_6_timing(link: serial.Serial, settle: float) -> None:
    """Find the shortest gap between writes this sign will actually accept."""
    print("\nStep 6: how much settling time the sign actually needs")
    print("  The old service slept 2 seconds after every write, a number nobody")
    print("  measured. The protocol's own inter-byte timeout is %.0fs."
          % c.INTER_BYTE_TIMEOUT_SECONDS)

    for gap in (1.0, 0.5, 0.25, 0.1, 0.05):
        print("\n  gap %.2fs" % gap)
        for index in range(6):
            send(
                link,
                frames.write_text_file(b"A", render("GAP %d-%d" % (int(gap * 100), index))),
                label="write file A",
                settle=gap,
            )
        answer = ask("At a %.2fs gap, did every write land correctly? [y/n]" % gap)
        if answer.lower().startswith("n"):
            print("  -> %.2fs is too fast. Configure inter_packet_delay above it." % gap)
            break

    send(
        link,
        frames.write_text_file(b"A", render("<red>ONE")),
        label="restore file A",
        settle=settle,
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
        help="pyserial URL for the sign, such as socket://192.168.2.51:4001 or /dev/ttyUSB0",
    )
    parser.add_argument("--baud", type=int, default=9600, help="line speed, default 9600")
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to wait after each write outside the timing step, default 2.0",
    )
    parser.add_argument(
        "--confirm-erase",
        action="store_true",
        help="required, because step 2 erases every message on the sign",
    )
    args = parser.parse_args()

    if not args.confirm_erase:
        parser.error(
            "this spike erases every message on the sign. Stop the service and the "
            "crontab line first, then pass --confirm-erase."
        )

    print("readerboard protocol spike")
    print("Sign: %s at %d baud" % (args.url, args.baud))

    link = step_1_transport(args.url, args.baud, args.settle)
    try:
        step_2_memory(link, args.settle)
        step_3_rotation(link, args.settle)
        step_4_priority(link, args.settle)
        step_5_reads(link)
        step_6_timing(link, args.settle)
    finally:
        link.close()

    print("\n" + "=" * 78)
    print("What the sign did")
    print("=" * 78)
    for question, answer in observations:
        print("  %-62s %s" % (question, answer))
    print("\nPaste this into docs/protocol-notes.md and update the status heading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
