#!/usr/bin/env python3
"""Settle against real hardware the questions the protocol document cannot answer.

The wire formats this service uses are quoted from the Alpha Sign
Communications Protocol and are not in doubt. What the document cannot say is
how your particular BetaBrite Classic behaves at the end of an Ethernet to
RS-232 adapter. Ten things have been genuinely open, and this script is how each
was put to the sign. The first seven are settled, across sessions on 2026-09-09,
2026-09-11 and 2026-09-12, and their answers are in docs/protocol-notes.md.
Running it again re-confirms them on the sign in front of you, which is worth
doing: one answer has already been recorded wrongly once and caught on a repeat.
The last three are open, and steps 9 to 11 are where they are asked.

1. Is the rotation seamless on this sign, with no blanking between files?
2. Does rewriting only the run sequence disturb the display? A slot expiring
   does exactly that, every time. It does, briefly, and much less than a TEXT
   file write does; it was recorded as not disturbing it at all until somebody
   watched several runs.
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
8. What does the sign draw for a file the memory configuration allocated and
   nothing has ever written? Open. Nothing has ever named one in a run sequence,
   because the service writes a file before it names it.
9. What does it do when every file the sequence names is empty? Open, and it is
   not question 5: the sign is still being told to play files, they just have
   nothing in them. And does a file the sign is skipping start playing when it
   is written, with the sequence left alone?
10. What does a sequence of mostly empty files cost? Open. Question 7 says such
    a file is passed over; this asks what passing over the whole rest of the
    pool on every turn does to a sign holding one message.

The last three are here because of a change being weighed. If an empty file
really is passed over, the run sequence could name every file in the pool all
the time and be rewritten only when a message is hidden or the running order
changes. Creating a message would then be one TEXT file write rather than a
TEXT file write with a sequence write landing on top of it, and that combination
is what reads as a stutter rather than as one interruption. Whether it works
rests on these three, and on the second half of question 9, which nobody had
thought to doubt: the whole idea assumes the sign notices a file filling up
underneath a sequence that already names it.

It also measures how long the sign really needs between packets, which the old
service never did; it just slept two seconds.

This script is destructive. Step 2 writes a memory configuration, and that
erases every message on the sign. It therefore refuses to run without
--confirm-erase. It allocates eight files by default, the service's own
slot_count, because the last three steps need files that nothing has written;
steps 3 to 8 use A, B and C exactly as they always have. --pool changes how
many are allocated, up to the 26 the service allows.

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

# The three files every settled step uses, and which must stay the front of the
# allocated pool: steps 9 to 11 take everything after them as the files nothing
# has written.
POOL = [b"A", b"B", b"C"]
POOL_SIZE_DEFAULT = 8
SLOT_CAPACITY = 256
READ_LIMIT_BYTES = 4096

observations: list[tuple[str, str]] = []


def labels_as_text(labels: list[bytes]) -> str:
    """Render file labels for a printed line or a question."""
    return " ".join(label.decode("ascii") for label in labels)


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


def step_2_memory(link: serial.Serial, settle: float, pool: list[bytes]) -> None:
    """Allocate the file pool, and confirm that doing so erases the sign."""
    print("\nStep 2: set the memory configuration (this erases the sign)")
    allocations = [frames.FileAllocation(label, SLOT_CAPACITY) for label in pool]
    print("  allocating %s" % labels_as_text(pool))
    print("  claiming %d bytes of the memory pool" % frames.memory_claimed(allocations))
    print("  Steps 3 to 8 use A, B and C and nothing else. Everything after them is")
    print("  allocated and then left alone, which is exactly what steps 9 to 11 need:")
    print("  files this run has never written, and enough of them to leave a sequence")
    print("  mostly empty.")
    print("  Watch the display now, before this goes out. The blank is brief: a run")
    print("  on 2026-09-11 missed it and recorded the wrong answer, and it took two")
    print("  more attempts to see it.")
    send(
        link,
        frames.set_memory_config(allocations),
        label="allocate %d files" % len(pool),
        settle=settle,
    )
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

    print("\n  Now only the run sequence changes, which is what a slot expiring or")
    print("  being hidden does. It does disturb the display, but briefly: much less")
    print("  than the blank a TEXT file write causes, and short enough to vanish into")
    print("  any other change on screen. It was recorded as causing none at all until")
    print("  somebody watched several runs, so watch a static moment and watch closely.")
    send(link, frames.set_run_sequence([b"A", b"C"]), label="run sequence A C", settle=settle)
    ask("Has TWO dropped out, leaving ONE and THREE? [y/n]")
    ask("How much did the display flinch? [none/brief/full restart/other]")
    ask("Was it smaller than the blank a TEXT file write causes? [y/n]")

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


def step_9_unwritten_files(link: serial.Serial, settle: float, spare: list[bytes]) -> None:
    """Ask what a file the sign allocated and nothing ever wrote draws."""
    print("\nStep 9: files the sign allocated and nothing has ever written")
    print("  %s were allocated in step 2 and nothing has touched them since." % labels_as_text(spare))
    print("  The sign was erased before that configuration landed, so they are as")
    print("  empty as a file can be: allocated, never written, not even blanked.")
    print("  Nothing has ever named a file in that state in a run sequence, because")
    print("  the service writes a file before it names it. A sequence that always")
    print("  named the whole pool would name them from the moment the pool existed,")
    print("  so what they draw decides whether such a sequence has to blank the pool")
    print("  once after every reconfiguration.")

    print("\n  First they are named on their own. Blank and frozen both mean they draw")
    print("  nothing, which is what step 4 got from a sequence naming nothing at all.")
    print("  Anything readable on the sign means they hold something of their own, and")
    print("  the pool would have to be blanked before it could safely be named.")
    send(
        link,
        frames.set_run_sequence(spare),
        label="run sequence, %d unwritten" % len(spare),
        settle=settle,
    )
    print("\n  Watch for half a minute. Nothing more is being sent.")
    ask(
        "With only never-written files named, what is on the sign? "
        "[blank/frozen on ONE, TWO or THREE/text of some kind/other]"
    )

    print("\n  Now one of them is named between two files that do have text. Step 5")
    print("  found a file written empty is passed over; this asks whether one that")
    print("  was never written is passed over too. TWO and THREE cycling straight")
    print("  past it is one answer. A turn of its own is the other, and that is the")
    print("  one that would put a gap in every rotation the service ever runs.")
    send(
        link,
        frames.set_run_sequence([b"B", spare[0], b"C"]),
        label="run sequence B %s C" % spare[0].decode(),
        settle=settle,
    )
    print("\n  Watch several full cycles rather than one.")
    ask(
        "What does %s's turn look like? [skipped/blank turn/text of some kind/other]"
        % spare[0].decode()
    )

    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    ask("Has the ONE, TWO, THREE rotation come back? [y/n]")


def step_10_files_that_empty_and_fill(link: serial.Serial, settle: float) -> None:
    """Ask what an all-empty sequence shows, and whether a file filling up starts playing."""
    print("\nStep 10: a sequence whose files empty out and fill up again")
    print("  The sequence names A, B and C for the whole of this step and is never")
    print("  rewritten. Only the contents of the files change.")

    print("\n  First, all three are emptied. This is not step 4's empty sequence,")
    print("  which is a different command: here the sign is still being told to play")
    print("  three files and every one of them has nothing in it. Step 5 found a")
    print("  single empty file passed over while others played, and what the sign")
    print("  does when there is nothing left to pass to has never been asked.")
    print("  Blank is one answer and frozen on the last message drawn is the other.")
    print("  Which it is decides whether a sequence that always names the whole pool")
    print("  still has to blank the last file by hand to clear the display.")
    ask("Ready to watch A, B and C be emptied one after another? [enter]")
    for label in POOL:
        send(
            link,
            frames.write_text_file(label, b""),
            label="empty file %s" % label.decode(),
            settle=settle,
        )
    print("\n  Give it half a minute before answering.")
    ask("With every named file empty, what is on the sign? [blank/frozen/other]")

    print("\n  Second, and the whole idea rests on this one: a file the sign is")
    print("  already skipping is written, and the sequence is left alone. Today the")
    print("  service writes the file and then names it, two packets and two")
    print("  disturbances one on top of the other. If a sequence that already names")
    print("  the file picks the text up by itself, the second packet is unnecessary.")
    print("  If it does not, nothing else in this run matters.")
    ask("Ready to watch A be written, with the sequence untouched? [enter]")
    send(link, frames.write_text_file(b"A", render("<red>ONE")), label="write file A", settle=settle)
    print("\n  Give it half a minute.")
    ask("Did ONE start showing, with no run sequence write? [y/n]")
    ask("How long did it take to appear? [at once/within a turn/longer/never]")

    print("\n  And again with the other two, so that a rotation grows from nothing to")
    print("  three messages without the sequence being touched once.")
    send(
        link, frames.write_text_file(b"B", render("<green>TWO")), label="write file B", settle=settle
    )
    ask("Are ONE and TWO both cycling now? [y/n]")
    send(
        link,
        frames.write_text_file(b"C", render("<amber>THREE")),
        label="write file C",
        settle=settle,
    )
    ask("And all three, the rotation step 3 showed? [y/n]")


def step_11_long_sequence(link: serial.Serial, settle: float, pool: list[bytes]) -> None:
    """Measure what it costs the sign to pass over a sequence that is mostly empty."""
    alone = len(pool) - 1
    beside_three = len(pool) - len(POOL)

    print("\nStep 11: what a sequence full of empty files costs")
    print("  Steps 5 and 9 ask whether an empty file is passed over. This asks what")
    print("  passing over it costs. A sequence that always named the whole pool would")
    print("  name %d files. On a sign holding one message, %d of them are" % (len(pool), alone))
    print("  empty and passed over on every turn. If each one costs the sign a beat,")
    print("  a single held message gains a hitch it does not have today, which is the")
    print("  very thing the change is meant to remove.")
    print("  Step 5's answer is one observation, and the note recorded against it says")
    print("  a short enough blank would look like a skip. This is where that gets")
    print("  watched at length rather than once.")

    print("\n  The baseline first: one full file, named on its own. Every file this")
    print("  script writes is written in HOLD mode, so a sign with nothing else to do")
    print("  should be completely still.")
    send(link, frames.write_text_file(b"B", b""), label="empty file B", settle=settle)
    send(link, frames.write_text_file(b"C", b""), label="empty file C", settle=settle)
    send(link, frames.set_run_sequence([b"A"]), label="run sequence A", settle=settle)
    print("\n  Watch it for half a minute.")
    ask("Is ONE completely still, with nothing happening at all? [y/n]")

    print("\n  Now the same single message with every other file in the pool named")
    print("  around it. Nothing else changes: A still holds ONE and the other %d are" % alone)
    print("  empty. Watch for a full minute. What is being looked for is a repeating")
    print("  flicker, a redraw or a pause, anything that was not there a moment ago.")
    send(
        link,
        frames.set_run_sequence(pool),
        label="run sequence, whole pool",
        settle=settle,
    )
    ask("Is ONE still completely still? [y/n]")
    ask("If it is not, what happens and how often? [describe]")
    ask(
        "Against the flinch a run sequence write causes, how big is it? "
        "[nothing at all/smaller/about the same/larger]"
    )

    print("\n  And with content in three of them, which is step 3's rotation with %d" % beside_three)
    print("  empty files threaded through it. Step 3 had the same three messages with")
    print("  nothing between them, so that is what the comparison is against.")
    send(
        link, frames.write_text_file(b"B", render("<green>TWO")), label="write file B", settle=settle
    )
    send(
        link,
        frames.write_text_file(b"C", render("<amber>THREE")),
        label="write file C",
        settle=settle,
    )
    print("\n  Watch several full cycles.")
    ask("Does the rotation run as cleanly as it did in step 3? [y/n]")
    ask(
        "If there is a pause where the empty files are, how long is it? "
        "[none/shorter than a message/about a message/longer]"
    )

    # Hand the sign back on the rotation every other step leaves it on, rather
    # than on a sequence naming files nobody wrote.
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)


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
        "--pool",
        type=int,
        default=POOL_SIZE_DEFAULT,
        help="how many TEXT files to allocate, default %d, which is the service's own "
        "slot_count. Steps 3 to 8 use the first three whatever this is; steps 9 to 11 "
        "need the rest, left empty. Raise it to %d to see what the largest pool the "
        "service allows costs." % (POOL_SIZE_DEFAULT, len(c.TEXT_FILE_LABELS)),
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
    if not len(POOL) < args.pool <= len(c.TEXT_FILE_LABELS):
        parser.error(
            "--pool must be between %d and %d. The first %d files are what steps 3 to 8 "
            "use, and steps 9 to 11 need at least one more that nothing has written."
            % (len(POOL) + 1, len(c.TEXT_FILE_LABELS), len(POOL))
        )

    pool = list(c.TEXT_FILE_LABELS[: args.pool])
    spare = pool[len(POOL) :]

    print("readerboard protocol spike")
    print("Sign: %s at %d baud" % (args.url, args.baud))
    print("Pool: %d files, %s" % (len(pool), labels_as_text(pool)))

    link = step_1_transport(args.url, args.baud, args.settle)
    try:
        step_2_memory(link, args.settle, pool)
        step_3_rotation(link, args.settle)
        step_4_empty_sequence(link, args.settle)
        step_5_empty_file(link, args.settle)
        step_6_priority(link, args.settle)
        step_7_reads(link)
        step_8_timing(link, args.settle)
        step_9_unwritten_files(link, args.settle, spare)
        step_10_files_that_empty_and_fill(link, args.settle)
        step_11_long_sequence(link, args.settle, pool)
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
