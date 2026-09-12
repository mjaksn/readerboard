#!/usr/bin/env python3
"""Settle against real hardware the questions the protocol document cannot answer.

The wire formats this service uses are quoted from the Alpha Sign
Communications Protocol and are not in doubt. What the document cannot say is
how your particular BetaBrite Classic behaves at the end of an Ethernet to
RS-232 adapter. Seven things have been genuinely open, and this script is how
each was put to the sign. All seven are settled now, across sessions on
2026-09-09, 2026-09-11 and 2026-09-12, and their answers are in
docs/protocol-notes.md. Running it again re-confirms them on the sign in front
of you, which is worth doing: one answer has already been recorded wrongly once
and caught on a repeat.

1. Is the rotation seamless on this sign, with no blanking between files?
2. Does rewriting only the run sequence disturb the display? A slot expiring
   does exactly that, every time.
3. Does a run sequence write cancel a running priority message? The document
   says a write to the run time or run day table does, and is silent about the
   run sequence. The alert survived, which is why the service now sends these
   writes whatever is on the display.
4. Does the sign answer read commands through the adapter? If it does,
   divergence can be detected by asking rather than by re-pushing on a timer.
5. What does an empty run sequence show? The sign is told to play nothing while
   its files still hold their text. It freezes on the message it was drawing
   and holds it, rather than blanking.
6. Does emptying that frozen file clear the display? Hiding or deleting the
   last message rewrites the sequence and then empties the file, so the
   emptying is the only thing left that can end the freeze. It does: the sign
   went blank.
7. What does an empty file do when the sequence names it beside full ones? The
   sign passes over it, with no blank turn of its own, the same treatment the
   document gives a label with no file at all.

It also measures how long the sign really needs between packets, which the old
service never did; it just slept two seconds.

This script is destructive. Step 2 writes a memory configuration, and that
erases every message on the sign. It therefore refuses to run without
--confirm-erase.

Run it with the sign in front of you. It pauses to ask what you saw, then prints
a summary to paste into docs/protocol-notes.md.

    python scripts/protocol_spike.py --url socket://192.168.2.51:4001 --confirm-erase

Nothing else may be talking to the sign while this runs. Stop the service, and
anything else that writes to the sign, first.
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
READ_LIMIT_BYTES = 4096

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
    # Drained rather than read once: over socket:// in_waiting is 1 whenever
    # anything is waiting, so one read of it returns a lone byte. Capped, so a
    # link that never stops delivering cannot keep this going for ever.
    received = bytearray()
    budget = READ_LIMIT_BYTES
    while budget > 0 and (waiting := link.in_waiting):
        asked = min(waiting, budget)
        received += link.read(asked)
        budget -= asked
    reply = bytes(received)
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
    print("  Watch the display now, before this goes out. The blank is brief: a run")
    print("  on 2026-09-11 missed it and recorded the wrong answer, and it took two")
    print("  more attempts to see it.")
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


def step_4_empty_sequence(link: serial.Serial, settle: float) -> None:
    """Find out what the sign shows when it is told to play nothing at all."""
    print("\nStep 4: an empty run sequence, with the files left in place")
    print("  The three files still hold ONE, TWO and THREE, and only the sequence")
    print("  changes. It is emptied rather than shortened, which step 3 covered.")
    print("  The document does not say what that shows: a blank display, the last")
    print("  message frozen on it, or something of the sign's own. A message")
    print("  deactivated rather than deleted rests on the answer, and so does")
    print("  clearing every message at once.")
    send(link, frames.set_run_sequence([]), label="run sequence, empty", settle=settle)
    print("\n  Watch it for half a minute before answering. Nothing more is being sent.")
    print("  These files are held rather than scrolled, so the question is what the")
    print("  sign does when the message it is holding ends and the sequence names")
    print("  nothing to follow it. Frozen for one turn and then blank is a different")
    print("  answer from frozen, so give it long enough to tell them apart.")
    ask("With an empty sequence, what is on the sign? [blank/frozen/other]")

    # Read after the question, not before it. A read can disturb the display,
    # and what is on the display is the whole point of the step.
    reply = read_back(link, frames.read_run_sequence(), label="read run sequence (F.)")
    note("Run sequence read back while empty", repr(reply) if reply else "nothing")

    print("\n  Now the same three files are named again. Nothing has been rewritten,")
    print("  so if the rotation comes back whole, the sign kept every file through")
    print("  the empty sequence and reactivating a message costs one packet and no")
    print("  redraw of the message itself.")
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    print("\n  Give it a full cycle before answering, so that all three are seen.")
    ask("Has the rotation come back, with ONE, TWO and THREE all there? [y/n]")
    ask("Did any of the three lose its text or its colour? [y/n]")


def step_5_empty_file(link: serial.Serial, settle: float) -> None:
    """Two things the service assumes about an empty file, neither of them measured."""
    print("\nStep 5: an empty file, frozen on and then rotated through")
    print("  Step 4 showed the sign freezes when the sequence names nothing. Two")
    print("  things follow from that which nothing has ever checked, and the")
    print("  service leans on both.")

    print("\n  First: does emptying the file the sign is frozen on clear the display?")
    print("  Every path that hides or removes the last message rewrites the sequence")
    print("  and then empties the file, and the emptying is the only thing that can")
    print("  end the freeze. If it does not end it, the last message stays up for")
    print("  good and nothing the service can send will take it down.")
    print("  The sequence is set to A alone first, so which file the sign freezes on")
    print("  is known rather than whichever one it happened to reach.")
    send(link, frames.set_run_sequence([b"A"]), label="run sequence A", settle=settle)
    ask("Is ONE on the sign by itself? [y/n]")

    send(link, frames.set_run_sequence([]), label="run sequence, empty", settle=settle)
    ask("Frozen on ONE, as step 4 found? [y/n]")

    print("\n  Now it is emptied underneath the freeze. Watch the sign from the moment")
    print("  you answer the next question: the write goes out first and the question")
    print("  after it comes %.2gs later, which is long enough to miss a change." % settle)
    ask("Ready to watch file A be emptied? [enter]")
    send(link, frames.write_text_file(b"A", b""), label="empty file A", settle=settle)
    print("\n  Give it half a minute. Blank is the answer the service is built on.")
    print("  Still showing ONE means the freeze outlives its own file, and that")
    print("  hiding or deleting the last message cannot clear the sign at all.")
    ask("With its file emptied, what is on the sign? [blank/still ONE/other]")

    print("\n  Second: what does an empty file do when it is named alongside full")
    print("  ones? A is empty now and B and C still hold TWO and THREE, so naming all")
    print("  three asks it directly. The service refuses an empty message and tells")
    print("  callers, in the message field's own description, that the sign would")
    print("  cycle to the file and show nothing there. Nobody has ever checked that.")
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    print("\n  Watch several full cycles rather than one. The answers differ by what")
    print("  happens where ONE used to be: a visible blank turn of its own, A passed")
    print("  over so TWO and THREE cycle straight past it, or the display stuck.")
    ask("With A empty, what does its turn look like? [blank turn/skipped/frozen/other]")
    ask("Do TWO and THREE still cycle normally either side of it? [y/n]")

    # Put step 3's state back. Step 6 takes the sign over and hands it back, and
    # it expects a rotation to be there for both halves of that.
    send(
        link,
        frames.write_text_file(b"A", render("<red>ONE")),
        label="restore file A",
        settle=settle,
    )
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    ask("Has ONE come back, with all three cycling again? [y/n]")


def step_6_priority(link: serial.Serial, settle: float) -> None:
    """Confirm takeover, release, and whether a run sequence write cancels an alert."""
    print("\nStep 6: priority takeover and release")
    send(
        link,
        frames.write_text_file(c.FILE_PRIORITY, render("<red>ALERT")),
        label="priority write",
        settle=settle,
    )
    ask("Has ALERT taken over the whole sign, with the rotation stopped? [y/n]")

    print("\n  The important one. The document says a write to the run time table or")
    print("  the run day table cancels a running priority message, and says nothing")
    print("  either way about the run sequence.")
    print("  It survived on 2026-09-11, which is why the service sends these writes")
    print("  whatever is on the sign. This step re-confirms that on the sign in front")
    print("  of you.")
    send(link, frames.set_run_sequence([b"A", b"B"]), label="run sequence A B", settle=settle)
    ask("Is ALERT still on the sign after that run sequence write? [y/n]")

    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    send(link, frames.clear_priority_file(), label="release priority", settle=settle)
    ask("Has the rotation resumed on its own? [y/n]")


def step_7_reads(link: serial.Serial) -> None:
    """Find out whether the sign answers read commands through this adapter."""
    print("\nStep 7: can the sign be asked what it is holding?")
    print("  This sign answered all four of these on 2026-09-09, so the adapter")
    print("  carries traffic both ways. Re-proving it is cheap, and divergence")
    print("  could be detected by asking rather than re-pushing on a timer.")

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


def step_8_timing(link: serial.Serial, settle: float) -> None:
    """Find the shortest gap between writes this sign will actually accept."""
    print("\nStep 8: how much settling time the sign actually needs")
    print("  inter_packet_delay defaults to 0.25s, which is what a BetaBrite Classic")
    print("  was measured taking six writes in a row at on 2026-09-11, the same run")
    print("  failing at 0.1s. This finds the figure for the sign in front of you.")
    print("  The protocol's own inter-byte timeout is %.0fs." % c.INTER_BYTE_TIMEOUT_SECONDS)

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
            "this spike erases every message on the sign. Stop the service, and "
            "anything else that writes to it, then pass --confirm-erase."
        )

    print("readerboard protocol spike")
    print("Sign: %s at %d baud" % (args.url, args.baud))

    link = step_1_transport(args.url, args.baud, args.settle)
    try:
        step_2_memory(link, args.settle)
        step_3_rotation(link, args.settle)
        step_4_empty_sequence(link, args.settle)
        step_5_empty_file(link, args.settle)
        step_6_priority(link, args.settle)
        step_7_reads(link)
        step_8_timing(link, args.settle)
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
