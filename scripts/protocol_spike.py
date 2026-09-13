#!/usr/bin/env python3
"""Settle against real hardware the questions the protocol document cannot answer.

The wire formats this service uses are quoted from the Alpha Sign
Communications Protocol and are not in doubt. What the document cannot say is
how your particular BetaBrite Classic behaves at the end of an Ethernet to
RS-232 adapter. Eleven things have been genuinely open, and this script is how
each was put to the sign. Ten are settled, across sessions on 2026-09-09,
2026-09-11 and 2026-09-12, and their answers are in docs/protocol-notes.md.
Running it again re-confirms them on the sign in front of you, which is worth
doing: two of the answers were recorded wrongly the first time and caught on a
repeat, and both were about something brief on the display. The eleventh is
asked by step 7 and has no answer yet.

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
7. What does an empty file do when the sequence names it beside full ones? It
   gets no turn of its own, which the first run read as costing nothing. A
   second run the same day found the rest of it: the message before it holds
   about five seconds longer. No blank turn, but a gap all the same, taken out
   of the previous message rather than shown as one.
8. What does the sign draw for a file the memory configuration allocated and
   nothing has ever written? Nothing, and it behaves exactly as one written
   empty does, so an allocated pool needs no blanking.
9. What does it do when every file the sequence names is empty? It blanks, which
   is not question 5: a sequence naming nothing freezes, a sequence naming only
   empty files blanks. And a file the sign is skipping does start playing the
   moment it is written, with the sequence left alone.
10. What does a sequence of mostly empty files cost? About five seconds of dwell
    on the message before the empty run. A sign holding one message with the
    whole pool named around it pays nothing, since it has nowhere to rotate to.
11. What does a read cost the display? Open. Step 7 has sent all four special
    function reads to this sign twice and only ever asked whether a reply came
    back. The one measurement anywhere near it is a STRING file read on
    2026-09-10, which blanked the display briefly mid-scroll, and that single
    result has been standing in for every kind of read. It should not: a STRING
    is buffered inline into whatever message calls it, while a special function
    read asks the sign about its own tables and touches no file a message is
    drawing. Step 7 now asks it five ways, because the answer plausibly differs
    between them: the four special function reads against a held message, the
    same four against a scrolling one, a TEXT file read of a file not named in
    the run sequence, a STRING file read of a value no message calls, and then
    every one of those reads again with the display scrolling a message whose
    whole text lives in that STRING file. The last case is what separates a read
    costing something in itself from a read costing something only when it asks
    about the file the sign is drawing from, and the whole question of whether
    the sign can be polled turns on which of those it is.

Questions 8, 9 and 10 were asked for a change that was then dropped: naming
every file in the pool all the time, so that creating a message would be one
TEXT file write rather than a TEXT file write with a sequence write landing on
top of it. The sign turned out to do everything the idea needed, question 9's
second half included, but question 10 priced it. Carrying every unused file as
dwell on the message before it is a wash against the packet it saves.
docs/protocol-notes.md has the whole of that reasoning; it is kept because the
next person to have the idea should see it was measured rather than assumed.

It also measures how long the sign really needs between packets, which the old
service never did; it just slept two seconds.

This script is destructive. Step 2 writes a memory configuration, and that
erases every message on the sign. It therefore refuses to run step 2 without
--confirm-erase. It allocates eight files by default, the service's own
slot_count, because the last three steps need files that nothing has written;
steps 3 to 8 use A, B and C exactly as they always have. --pool changes how
many are allocated, up to the 26 the service allows.

--steps runs part of it: --steps 7, --steps 3-5, --steps 7,9,11, --steps 5- for
that step onwards. The steps hand state to each other, so two things follow.
Leaving step 2 out erases nothing and takes the sign's existing memory
configuration on trust, which is the way to re-ask one question without
flattening the sign. And a run starting above step 3 writes the three files
step 3 normally leaves behind, because every step above it expects to find
them. Step 9 is the one step a partial run cannot fully honour: without step 2
in the same run, nothing can promise its files were never written.

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

# What those three files hold. Every step from 4 on expects to find them, and
# names them in its questions, so they are one constant rather than a literal
# in each place that rewrites them.
MESSAGES = ("<red>ONE", "<green>TWO", "<amber>THREE")
LAST_STEP = 11

# Step 7 needs a message long enough to keep moving, so that a read landing
# mid-scroll has something to interrupt.
SCROLL_TEXT = "<green>SCROLLING WHILE THE SIGN IS ASKED WHAT IT IS HOLDING"

# One STRING file, allocated by step 2 and written by step 7, which reads it
# twice: once while nothing calls it, and once while it is the whole of what the
# sign is drawing. Capacity is the protocol's own ceiling so the second value
# can be long enough to scroll.
STRING_POOL = [b"a"]
STRING_CAPACITY = c.STRING_FILE_CAPACITY
STRING_VALUE = "IDLE"
STRING_SCROLL_VALUE = "<amber>THIS WHOLE MESSAGE LIVES IN A STRING FILE AND IS BEING READ"

observations: list[tuple[str, str]] = []


def labels_as_text(labels: list[bytes]) -> str:
    """Render file labels for a printed line or a question."""
    return " ".join(label.decode("ascii") for label in labels)


def parse_steps(spec: str) -> set[int]:
    """Turn ``7``, ``3-5``, ``7,9,11`` or ``5-`` into the steps to run.

    Raises :class:`ValueError` with something a person can act on, which the
    caller hands to ``parser.error``.
    """
    chosen: set[int] = set()
    for piece in (part.strip() for part in spec.split(",")):
        if not piece:
            continue
        if "-" in piece:
            low, _, high = piece.partition("-")
            first = _step_number(low or "1")
            last = _step_number(high) if high.strip() else LAST_STEP
            if last < first:
                raise ValueError("%r runs backwards; write it low to high" % piece)
            chosen.update(range(first, last + 1))
        else:
            chosen.add(_step_number(piece))
    if not chosen:
        raise ValueError("no steps were named")
    return chosen


def _step_number(text: str) -> int:
    """Read one step number, refusing anything outside the script."""
    try:
        number = int(text.strip())
    except ValueError:
        raise ValueError("%r is not a step number" % text.strip()) from None
    if not 1 <= number <= LAST_STEP:
        raise ValueError("there is no step %d; they run from 1 to %d" % (number, LAST_STEP))
    return number


def note(question: str, answer: str) -> None:
    """Record something observed, for the summary at the end."""
    observations.append((question, answer))


def ask(question: str) -> str:
    """Put a question to the person watching the sign and record the answer."""
    print()
    answer = input("  %s " % question).strip()
    note(question, answer or "(no answer)")
    return answer


def pause(prompt: str) -> None:
    """Wait for the operator without recording anything.

    Separate from :func:`ask` because these carry no answer, and a run makes
    enough of them that putting each one in the summary as "(no answer)" would
    bury the observations that matter.
    """
    print()
    input("  %s [enter] " % prompt)


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


def watched_read(link: serial.Serial, payload: bytes, *, label: str) -> bytes:
    """Wait for the operator, then send one read.

    Every read step 7 asks about goes through here. Sent back to back they take
    a couple of seconds each and a disturbance cannot be pinned on any one of
    them; one at a time, with the operator watching before each goes out, the
    question "which read did it" has an answer.
    """
    pause("Watch the display, then send the %s read." % label)
    return read_back(link, payload, label="read %s" % label)


def four_reads(link: serial.Serial) -> list[tuple[str, bytes]]:
    """Send the four special function reads one at a time, and return the answers.

    One helper because step 7 sends them three times over, and the comparison
    between those rounds is only worth anything if each sends the same four.
    """
    reads = (
        ("memory configuration", frames.read_memory_config(), "memory config (F$)"),
        ("memory pool size", frames.read_memory_pool_size(), "pool size (F#)"),
        ("run sequence", frames.read_run_sequence(), "run sequence (F.)"),
        ("run time table", frames.read_run_time_table(), "run time table (F))"),
    )
    return [(name, watched_read(link, payload, label=label)) for name, payload, label in reads]


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
    allocations = [frames.FileAllocation(label, SLOT_CAPACITY) for label in pool] + [
        frames.FileAllocation.string(label, STRING_CAPACITY) for label in STRING_POOL
    ]
    print("  allocating %s, and STRING %s" % (labels_as_text(pool), labels_as_text(STRING_POOL)))
    print("  The STRING file is for step 7, which reads one while nothing is calling")
    print("  it. No step displays it.")
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
    # Two questions rather than one. Asked together, a run that reached this
    # step with nothing on the display answers "no" for want of a message to
    # lose, and that reads afterwards as a third observation of no blank. It
    # happened on 2026-09-12.
    ask("Was there anything on the sign before that write? [y/n]")
    ask("Did the display blank, however briefly? [y/n/could not tell]")


def write_the_three_files(link: serial.Serial, settle: float) -> None:
    """Put ONE, TWO and THREE back in A, B and C and name all three."""
    for label, text in zip(POOL, MESSAGES, strict=True):
        send(
            link,
            frames.write_text_file(label, render(text)),
            label="write file %s" % label.decode(),
            settle=settle,
        )
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)


def establish_baseline(link: serial.Serial, settle: float) -> None:
    """Leave the sign in the state step 3 normally hands on.

    Only for a partial run that starts above step 3. Every step from 4 up
    expects to find A, B and C holding ONE, TWO and THREE with the sequence
    naming all three, because the step before it left them that way.
    """
    print("\nPreparing: the steps chosen start above step 3, which is what normally")
    print("  leaves A, B and C holding ONE, TWO and THREE with all three named.")
    print("  Writing that now so the steps below start where they expect to.")
    write_the_three_files(link, settle)
    ask("Is the sign cycling ONE, TWO, THREE? [y/n]")


def step_3_rotation(link: serial.Serial, settle: float) -> None:
    """Confirm the sign rotates several files by itself, without blanking."""
    print("\nStep 3: write three files and let the sign rotate them itself")
    write_the_three_files(link, settle)
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
    """Two things the service leans on about an empty file, both measured here."""
    print("\nStep 5: an empty file, frozen on and then rotated through")
    print("  Step 4 showed the sign freezes when the sequence names nothing. Two")
    print("  things follow from that which the service leans on, and this is where")
    print("  they were checked.")

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
    pause("Ready to watch file A be emptied")
    send(link, frames.write_text_file(b"A", b""), label="empty file A", settle=settle)
    print("\n  Give it half a minute. Blank is the answer the service is built on.")
    print("  Still showing ONE means the freeze outlives its own file, and that")
    print("  hiding or deleting the last message cannot clear the sign at all.")
    ask("With its file emptied, what is on the sign? [blank/still ONE/other]")

    print("\n  Second: what does an empty file do when it is named alongside full")
    print("  ones? A is empty now and B and C still hold TWO and THREE, so naming all")
    print("  three asks it directly. This one has been answered wrongly once: A gets")
    print("  no turn of its own, which the first run read as costing nothing, and it")
    print("  does not. The message before A in the cycle holds about five seconds")
    print("  longer, and that is what to watch for rather than a blank.")
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    print("\n  Watch several full cycles rather than one, and time the file before A")
    print("  against the other two rather than looking at where A would have been.")
    ask("With A empty, what does its turn look like? [blank turn/no turn at all/frozen/other]")
    ask("Does the message before A hold longer than the others, and by how long? [describe]")
    ask("Do TWO and THREE still cycle normally otherwise? [y/n]")

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


def step_7_reads(link: serial.Serial, settle: float) -> None:
    """Find out whether the sign answers read commands, and what asking costs the display."""
    print("\nStep 7: can the sign be asked what it is holding, and at what cost?")
    print("  This sign answered all four of these on 2026-09-09, so the adapter")
    print("  carries traffic both ways. Re-proving it is cheap, and divergence")
    print("  could be detected by asking rather than re-pushing on a timer.")

    print("\n  What has never been asked is what a read costs the display. Both")
    print("  earlier runs recorded only whether a reply came back. A STRING file")
    print("  read was measured on 2026-09-10 blanking the display briefly")
    print("  mid-scroll, and that one result is standing in for every kind of")
    print("  read, which it should not: a STRING is buffered inline into whatever")
    print("  message calls it, while these four ask the sign about its own tables")
    print("  and touch no file a message is drawing. They could differ either way.")

    print("\n  The display is put on one held message first, because a disturbance")
    print("  this small vanishes into a rotation changing by itself. Watch that")
    print("  message and nothing else.")
    send(link, frames.set_run_sequence([b"A"]), label="run sequence A", settle=settle)
    ask("Is ONE on the sign by itself, holding still? [y/n]")

    print("\n  The four reads go out back to back and take a few seconds. Watch the")
    print("  whole time rather than glancing at the end.")

    replies = dict(four_reads(link))

    ask("What did ONE do while those four reads went out? [nothing/flicker/blank/other]")
    ask(
        "If it moved, how did it compare with the blank a TEXT file write causes? "
        "[nothing at all/smaller/about the same/larger]"
    )
    ask("Which of the four did it? [all four/name them/none/could not tell]")

    # == the same four, against a message that is moving ====================
    print("\n  Now the same four reads against a message that is scrolling. This is")
    print("  the case the one existing measurement came from: the STRING read on")
    print("  2026-09-10 'blanked the display briefly mid-scroll and picked up from")
    print("  about where it was'. A scroll shows a different kind of damage from a")
    print("  held message. A held one can only blank; a scroll can also stall,")
    print("  jump, or start again from the right-hand edge, and a stall is easy to")
    print("  see precisely because everything else is moving.")
    send(
        link,
        frames.write_text_file(b"A", render(SCROLL_TEXT), mode=c.MODE_ROTATE),
        label="write file A, scrolling",
        settle=settle,
    )
    ask("Is A scrolling steadily across the display? [y/n]")
    print("\n  Watch the motion itself rather than the text.")

    four_reads(link)

    ask(
        "What did the scroll do while those four reads went out? "
        "[nothing/stalled and resumed/jumped/started again/blanked/other]"
    )
    ask("If it stalled, roughly how long for? [describe, or none]")
    ask("Which of the four did it? [all four/name them/none/could not tell]")

    # == files the sign is not drawing from =================================
    print("\n  Last, the contents of two files nothing on the display is using. This")
    print("  is the read a reconciliation scheme would actually make: it asks about")
    print("  a file the sign is not drawing from, so there is a fair chance it costs")
    print("  nothing at all even though a STRING read of a file in use does not.")
    send(
        link,
        frames.write_text_file(b"A", render(MESSAGES[0])),
        label="restore file A, held",
        settle=settle,
    )
    send(link, frames.set_run_sequence([b"A"]), label="run sequence A", settle=settle)
    send(
        link,
        frames.write_string_file(STRING_POOL[0], render(STRING_VALUE)),
        label="write STRING %s" % STRING_POOL[0].decode(),
        settle=settle,
    )
    print("\n  B holds TWO and is not named in the sequence. The STRING file %s holds"
          % STRING_POOL[0].decode())
    print("  a value no message calls. Neither is on the display, and ONE is held")
    print("  still in front of you. Watch ONE.")
    ask("Is ONE on the sign by itself, holding still? [y/n]")

    # frames.py has no TEXT file read, so this is built from the constants the
    # way the STRING spike built its own payloads. Adding the builder is real
    # work and does not belong in a spike commit.
    replies["text file B"] = watched_read(
        link, c.COMMAND_READ_TEXT + b"B", label="text file B (BB)"
    )
    replies["STRING file %s" % STRING_POOL[0].decode()] = watched_read(
        link,
        frames.read_string_file(STRING_POOL[0]),
        label="STRING %s, idle (H%s)" % (STRING_POOL[0].decode(), STRING_POOL[0].decode()),
    )

    ask("What did ONE do while those two reads went out? [nothing/flicker/blank/other]")
    ask(
        "Did the two differ from each other? "
        "[same/the text read was worse/the STRING read was worse/could not tell]"
    )

    # == the STRING the sign is actually drawing =============================
    print("\n  The hardest case, and the one that decides the rest. A is rewritten to")
    print("  hold nothing but the call to STRING %s, so the whole of what the sign is"
          % STRING_POOL[0].decode())
    print("  drawing lives in that STRING file, and it is set scrolling. Then the")
    print("  same reads go out, the STRING read among them.")
    print("  What this separates is whether a read costs anything in itself, or only")
    print("  when it asks about the file the sign is drawing from. The reads just")
    print("  now were of a text file and a STRING file sitting idle. These are the")
    print("  same two commands against the file on the display. If the idle ones were")
    print("  free and these are not, the rule is about what is being drawn rather")
    print("  than about reading, and a scheme that only ever reads idle files is safe.")
    send(
        link,
        frames.write_string_file(STRING_POOL[0], render(STRING_SCROLL_VALUE)),
        label="write STRING %s, long" % STRING_POOL[0].decode(),
        settle=settle,
    )
    # Built by hand rather than through the markup, so that A holds the call and
    # nothing else: no colour, no text of its own, just the insert and a label.
    send(
        link,
        frames.write_text_file(
            b"A", c.STRING_FILE_INSERT + STRING_POOL[0], mode=c.MODE_ROTATE
        ),
        label="write file A as a wrapper",
        settle=settle,
    )
    ask("Is the STRING's text scrolling across the display? [y/n]")

    four_reads(link)
    replies["text file A, the wrapper"] = watched_read(
        link, c.COMMAND_READ_TEXT + b"A", label="text file A, the wrapper (BA)"
    )
    replies["STRING %s, on the display" % STRING_POOL[0].decode()] = watched_read(
        link,
        frames.read_string_file(STRING_POOL[0]),
        label="STRING %s, the one on the display" % STRING_POOL[0].decode(),
    )

    ask(
        "What did the scroll do across all six of those reads? "
        "[nothing/stalled and resumed/jumped/started again/blanked/other]"
    )
    ask(
        "Which read did it, if you could tell? "
        "[the STRING read/the text read/one of the four/several/could not tell]"
    )
    ask(
        "Against reading the same STRING while nothing called it, a moment ago: "
        "[the same/worse now/better now/could not tell]"
    )

    # Back to the rotation the following steps expect.
    send(
        link,
        frames.write_text_file(b"A", render(MESSAGES[0])),
        label="restore file A, held",
        settle=settle,
    )
    send(link, frames.set_run_sequence(POOL), label="run sequence A B C", settle=settle)
    ask("Has the ONE, TWO, THREE rotation come back, with ONE held rather than scrolling? [y/n]")

    answered = [name for name, reply in replies.items() if reply]
    note("Reads that came back", ", ".join(answered) if answered else "none")
    print("\n  The sign answered %d of %d reads." % (len(answered), len(replies)))


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
    print("  found a file written empty gets no turn of its own but still holds the")
    print("  message before it several seconds longer. This asks whether one that was")
    print("  never written behaves the same way, and 2026-09-12 said it does, at about")
    print("  two seconds rather than five. So watch TWO rather than the gap after it.")
    send(
        link,
        frames.set_run_sequence([b"B", spare[0], b"C"]),
        label="run sequence B %s C" % spare[0].decode(),
        settle=settle,
    )
    print("\n  Watch several full cycles rather than one.")
    ask(
        "What does %s's turn look like? [no turn at all/blank turn/text of some kind/other]"
        % spare[0].decode()
    )
    ask("Does TWO hold longer than THREE does, and by how long? [describe]")

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
    print("  single empty file given no turn while the others played; this is what")
    print("  the sign does when there is nothing left to give a turn to.")
    print("  2026-09-12 said blank rather than frozen, which is the opposite of what")
    print("  step 4's empty sequence does and is the point of asking both.")
    pause("Ready to watch A, B and C be emptied one after another")
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
    pause("Ready to watch A be written, with the sequence untouched")
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
    print("  Steps 5 and 9 ask what an empty file does when it is named. This asks")
    print("  what it costs. A sequence that always named the whole pool would name")
    print("  %d files. On a sign holding one message, %d of them are empty" % (len(pool), alone))
    print("  on every turn, and this is the step that priced that and found it too")
    print("  expensive to be worth the packet it saves: about five seconds of dwell")
    print("  added to the message before the empty run, on 2026-09-12.")
    print("  A sign with only one message pays nothing, having nowhere to rotate to,")
    print("  so both cases are run here and they do not give the same answer.")

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

    print("\n  And with content in three of them, which is step 3's rotation with %d" % beside_three)
    print("  empty files threaded through it. Step 3 had the same three messages with")
    print("  nothing between them, so that is what the comparison is against. The cost")
    print("  lands on C, the last file with content before the empty run, rather than")
    print("  where the empty files are, so time C against ONE and TWO.")
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
    ask("How much longer does C hold than ONE and TWO do? [seconds, or none]")
    ask("Is the rotation otherwise as clean as it was in step 3? [y/n]")

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
        "--steps",
        default="1-%d" % LAST_STEP,
        help="which steps to run, as 7, or 3-5, or 7,9,11, or 5- for that one onwards. "
        "Default is all of them. Step 1 opens the link and always runs. Leave step 2 "
        "out and the sign keeps the memory configuration it already has, so nothing is "
        "erased; a run that starts above step 3 writes the three files that step 3 "
        "normally leaves behind, since every step above it expects to find them.",
    )
    parser.add_argument(
        "--confirm-erase",
        action="store_true",
        help="required, because step 2 erases every message on the sign",
    )
    args = parser.parse_args()

    try:
        chosen = parse_steps(args.steps)
    except ValueError as err:
        parser.error("--steps %s" % err)

    if 2 in chosen and not args.confirm_erase:
        parser.error(
            "step 2 erases every message on the sign. Stop the service, and anything "
            "else that writes to it, then pass --confirm-erase. Or leave step 2 out "
            "with --steps, which erases nothing."
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
    print("Steps: %s" % ", ".join(str(number) for number in sorted(chosen)))

    if 2 not in chosen:
        print("\nStep 2 is not being run, so nothing is erased and the sign keeps the")
        print("memory configuration it already has. Every step below assumes that")
        print("configuration allocates at least %s." % labels_as_text(pool))
    if 9 in chosen and 2 not in chosen:
        print("\nStep 9 asks what a file nothing has ever written draws, and without")
        print("step 2 in the same run nothing can promise that %s" % labels_as_text(spare))
        print("were never written. Read its answer as being about empty files rather")
        print("than untouched ones.")

    link = step_1_transport(args.url, args.baud, args.settle)
    try:
        after_2 = sorted(chosen - {1, 2})
        if 2 in chosen:
            step_2_memory(link, args.settle, pool)
        # Step 3 leaves the state every later step starts from, so a partial run
        # that skips past it has to put that state there itself.
        if after_2 and after_2[0] > 3:
            establish_baseline(link, args.settle)

        runners = {
            3: lambda: step_3_rotation(link, args.settle),
            4: lambda: step_4_empty_sequence(link, args.settle),
            5: lambda: step_5_empty_file(link, args.settle),
            6: lambda: step_6_priority(link, args.settle),
            7: lambda: step_7_reads(link, args.settle),
            8: lambda: step_8_timing(link, args.settle),
            9: lambda: step_9_unwritten_files(link, args.settle, spare),
            10: lambda: step_10_files_that_empty_and_fill(link, args.settle),
            11: lambda: step_11_long_sequence(link, args.settle, pool),
        }
        for number in after_2:
            runners[number]()
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
