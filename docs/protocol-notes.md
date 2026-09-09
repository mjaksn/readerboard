# Alpha protocol notes

What the Alpha Sign Communications Protocol actually says about the parts of it this
service depends on, with the quotation behind each claim.

**Status: confirmed against the protocol document. Not yet confirmed on the hardware.**
The wire formats below are quoted from the Alpha Sign Communications Protocol itself, so
they are no longer anybody's reading of anybody else's implementation. What the document
cannot tell us is how this particular BetaBrite Classic behaves at the end of an Ethernet
to RS-232 adapter. Four questions remain open, and they are listed at the end.
`scripts/protocol_spike.py` settles all four.

## Sources

- **Alpha Sign Communications Protocol**, Adaptive Micro Systems, form 9708-8061. This is
  the primary source and everything quoted below comes from it. The plan referred to form
  9708-8067; the document is 9708-8061.

  Two revisions are cited across this project, and they agree on every value used. The
  quotations in this file and the citations in `tests/test_constant_values.py` are from
  revision E, dated August 1 2003. `readerboard/protocol/constants.py` was regenerated
  from revision F, dated March 10 2006, which Adaptive publishes at
  `https://www.alpha-american.com/alpha-manuals/M-Protocol.pdf`. Their pagination differs
  by a page in places: the control code table is on page 80 of revision E and page 81 of
  revision F, while Table 15 is on page 21 of both. A page number that disagrees between
  those files is the revision rather than a mistake in either.
- `msparks/alphasign` and BBXML, consulted before the document was to hand. Both agreed
  with it. They are recorded here only because their agreement is what made it safe to
  start building before the document arrived.

## Packet frame

Proven on the hardware, and the one part of this that was never in doubt:

```
WAKEUP  SOH  type  address  STX  <command and payload>  EOT
```

The document specifies **five** nulls for the wakeup, describing them as what "cause a
sign to lock onto a baud rate". The constant here sends six. Six has driven this sign
for years, and more nulls than required is harmless, so it is left alone.

`SOH` is 0x01, `STX` 0x02, `EOT` 0x04, the sign type `^` for a BetaBrite and the address
`00` for broadcast.

## Set Memory Configuration

Special function label `$` (0x24), written with the `E` write-special command, so the
payload begins `E$`. The document's own words: "To Set Memory Configuration 11 (or
multiples thereof) ASCII characters are used to set a sign's Memory Configuration table."

The format is `FTPSIZEQQQQ`, eleven characters per file:

| Field | Width | Meaning |
| --- | --- | --- |
| `F` | 1 | File label. Any character 0x20 to 0x7E, though this service uses `A` to `Z`. |
| `T` | 1 | File type: `A` TEXT, `B` STRING, `D` DOTS picture. |
| `P` | 1 | `U` unlocked, `L` locked, meaning whether an infrared keyboard may edit it. |
| `SIZE` | 4 | File size in bytes, as uppercase hex. |
| `QQQQ` | 4 | For a TEXT file, a start time and a stop time. |

Three consequences that shape the design:

1. **Writing a memory configuration overwrites the previous table.** "Whenever a Memory
   Configuration is written, the previous table is overwritten." The service therefore
   allocates its whole pool in one write, records the applied plan in its state file, and
   reconfigures only when the plan itself changes. An ordinary message update must never
   touch `E$`.
2. **Nothing else can be written until it has been.** "A message file cannot be written
   until a Memory Configuration is written first, unless the file is a Priority TEXT file
   or the default TEXT file A." So a service that only ever writes the priority file can
   work without configuring memory at all, and one that uses the file pool cannot.
3. **Each file costs eleven bytes of overhead beyond its own size.** "The sum of all the
   file sizes plus 11 bytes of overhead for each file should not exceed the total amount
   of available memory in the pool." `Settings` counts that overhead when it checks a
   configured pool against the sign's capacity.

`E$` with nothing after it clears memory outright. `frames.clear_memory` spells that,
kept separate from `set_memory_config` so an empty list cannot wipe the sign by accident.

### A clear must come first, measured on the sign

On 2026-09-09 the real BetaBrite Classic, on an Ethernet to RS-232 adapter at
`socket://192.168.2.154:23`, refused to display from a memory configuration that was not
preceded by a bare `E$` clear. The configuration was accepted, stored and read back
verbatim through `F$`, and the sign then showed nothing from any file it named. Sending a
bare `E$` and then the identical configuration, on writes matching to the byte, made the
files play. Without the clear the sign stayed blank; with it the rotation ran. This was
confirmed with the service's own byte sequence, differing only by that one command.

So the document's "whenever a Memory Configuration is written, the previous table is
overwritten" is true of the stored table and not of the display: this sign needs the
table torn down before the new one takes. `SignController.apply_memory_config` therefore
sends `clear_memory` before `set_memory_config`. It adds no risk, because writing a
configuration already erases the sign, so clearing first reaches the same empty sign a
step sooner.

The clear puts the sign through a reset, and reads go unanswered through the first five
seconds or so of it. The configuration that follows, though, is accepted regardless: a
configuration written a second or more after the clear was seen to display once the sign
came back, so the sign buffers it through the reset and applies it on the way back. One
second is the shortest gap that was tried. `apply_memory_config` waits
`MEMORY_CLEAR_SETTLE_SECONDS`, two, which sits a little above the shortest gap that was
tried rather than at its edge. The wait is skipped when `inter_packet_delay` is zero,
which is how the simulator and the test transport are run, because neither has a reset to
sit through.

### The start and stop times

Appendix B encodes times in ten minute steps, `00` for midnight through to the small
hours of the following day, as two hex characters each. The value this service uses is
`FFFF`, and the appendix is explicit about what that means: "Stop Time is ignored when
Start Time is set to Always (FF)."

So a file allocated `FFFF` is always eligible, and naming it in the run sequence is the
only thing that decides whether it plays. That is what lets a slot expiring by TTL be
handled by rewriting the run sequence, with no memory reconfiguration and so no erasure.

## Priority TEXT file

File label `0` (0x30). The document: "A Priority TEXT file is a special 125-byte message
that does not need to be configured because it always exists on a sign. When data is
written to a Priority TEXT file, all other TEXT files that are currently running will
stop being displayed."

It runs alone until one of four things happens, of which the one that matters here is "a
Write Priority TEXT file without any ASCII Message is sent". Then: "Once a Priority TEXT
file stops running, the sign will begin running the other TEXT files."

That is precisely an alert: takeover, then release by writing an empty priority file, and
the rotation resumes by itself.

"Without any ASCII Message" turns out to mean without anything at all, and this was
measured the hard way on 2026-09-09. An ordinary text write is `A`, the label, a
Start-of-Message byte, a position, a mode, then the text. Sending that to file `0` with
the text empty, which reads as "no ASCII message" and is what the service did, does not
release: the sign takes it as a blank priority message and holds the screen on nothing.
The release is the bare write, `A0` and not a byte more. On the sign the bare form brought
the rotation straight back and the formatted-but-empty form blanked it. `clear_priority`
and `frames.clear_priority_file` send the bare form.

This was the bug behind the sign that showed alerts and never showed a slot. The service
clears the priority file on every start, to let go of an alert a previous run may have
left up, so from the first moment a blank priority message was suppressing every slot that
was ever written. It looked exactly like the rotation not working.

It is also the trap. Writing ordinary messages to file `0` is the obvious shortcut, and it
works right up until a second source wants the sign, at which point the protocol
guarantees only one of them is visible.

The 125 byte capacity is fixed and outside the memory pool. `frames.write_text_file`
rejects a longer priority write rather than letting the sign truncate it silently.

## Set Run Sequence

Special function label `.` (0x2E), so the payload begins `E.` and continues `KPF`:

| Field | Width | Meaning |
| --- | --- | --- |
| `K` | 1 | `T` run each file according to its own times, the default; `S` run them in order regardless of each file's run time; `D` as `T`, but delete each file when it reaches its off time. |
| `P` | 1 | `U` unlocked (default), `L` locked. |
| `F` | 1 each | The TEXT file labels to play, in order. |

From 3 to 130 characters in total, so up to 128 labels. The service sends `S`, since every
file it allocates is always eligible anyway and being explicit means a file that later
gains a schedule cannot silently change how the rotation behaves.

One detail worth having: "If a File Label is invalid or does not exist, the next File
Label will be processed." A stale label in a sequence is skipped rather than treated as an
error, which makes the run sequence forgiving of a race between a slot expiring and the
sequence being rewritten.

Once the sequence is set, the sign cycles the named files by itself, with no host
involvement and no serial traffic per rotation. This is what the design rests on:
rotation costs nothing, so several sources can share the sign without constant redraws.

## Soft reset, the non-destructive one

Table 15 gives `,` (2CH) as **Soft Reset**: "causes a soft reset of the sign. There is no
data in this field. A soft reset causes the sign to go through its power-up diagnostics.
Memory will not be cleared (non-destructive)."

Both halves of that were checked on the real sign on 2026-09-09 rather than taken on
trust, because the claim that matters is the second one. The sign visibly ran the same
self test and start-up sequence it runs when it is plugged in. Either side of the reset,
`F$`, `F#`, `F.` and reads of two occupied text files came back **byte for byte
identical**: the memory configuration, the pool's used and free counts, the run sequence
`E.SUABC`, and the rendered contents of files A and B including their colour codes. The
sign resumed cycling what it had been cycling. It was sent twice, with the same result.

So there are two resets in this protocol and they are a byte apart. `E$` tears the sign
down and erases it. `E,` restarts the sign and keeps everything. That makes the soft reset
the first thing to reach for on a sign whose decoder has wedged out of reach, and
`POST /sign/reboot`, which erases and rebuilds, the escalation when a restart alone is not
enough. `SOFT_RESET` is therefore safe to carry in the closed control command set, since
it disturbs no file the service is tracking.

One consequence worth knowing: the sign is deaf while it runs its diagnostics, so a write
sent into that window is not refused, it simply is not there afterwards. The control
command route waits the reset out before answering, so a 204 means the sign is listening
again rather than that bytes were sent.

## The sign's date has no century, so no token offers it

Table 15 gives `;` (3BH) as Set Date, six ASCII characters `mmddyy`, and Table 16 reads it
back in the same shape. Nothing in this service writes it, and that is deliberate rather
than an oversight, which is worth writing down because the absence looks exactly like a
gap somebody should close.

The year is two digits. Footnote 15 to that table says: "For Alpha protocol version 2.0
and greater, the year (yy) is windowed as follows: 00 to 96 = 2000 to 2096. 97 to 99 =
1997 to 1999." The windowing is gated to 2.0 and above. Table 3, Protocol version
comparison, lists the supported protocols per sign, and its Betabrite row reads
`Yes Yes Yes No No No No` against the columns EZ KEY II, Alpha 1.0, Alpha 2.0 and Alpha
3.0, the last two each split into their two parity variants. A Betabrite is EZ KEY II and
Alpha 1.0 only.

So this sign applies no century to the year it stores, and no value the service could send
makes it read as the present day. The `<date>`, `<date_dmy>` and `<date_long>` markup
tokens were therefore removed: each inserted the sign's own date, and each would have
drawn a confidently wrong one. `<date_long>` was the worst, rendering `MMM.DD, YYYY` from a
field with no century in it.

`<time>` and `<week_day>` stay, and the difference is that both are registers of their own
which `ClockService` writes at startup, hourly, and on every reconnect. The protocol's
date control codes remain in `constants.py` with their citations, and the sign simulator
still annotates them, so a message written by an older version stays readable.

## The speaker is a fixed-pitch buzzer

Table 15 gives `(` (28H) as Generate Speaker Tone, taking one to five characters: `A` and
`B` turn the speaker on and off, `0` is "a continuous tone for about 2 seconds", `1` is
"three, short beeps (total time about 2 seconds)", and `2` takes `FFDR`, a programmable
tone carrying a frequency `00`..`FE`, a duration in 0.1s steps, and a repeat count.
Options `3` and `4` are Alpha 2.0 and 3.0 only, so not this sign.

The programmable form is not exposed, and the reason is the hardware. On 2026-09-09 the
BetaBrite Classic was driven at `40` against `A0`, then at the extreme ends `00` against
`FE`, holding duration and repeat constant. Every tone sounded the same pitch. A suspected
loudness difference did not survive a controlled two-tone comparison either and was put
down to the earlier tones colouring the impression. So this sign has a fixed-pitch piezo
buzzer and drops the frequency byte.

Exposing `FFDR` would therefore promise a caller control the sign does not have, and a
parameter that is silently ignored is worse than one that was never offered. The service
exposes the two fixed sounds and nothing else, as `SOUND` with `TONE` or `BEEPS`.

Label `!` (21H) enables and disables the speaker, and Table 16 reads it back: `00` is
enabled, `FF` disabled, each a pair of ASCII characters rather than a byte. The document
calls disabled the default. This sign read as *enabled* without having been told to, which
is consistent with it beeping at power-up, so that documented default is not universal.

The service exposes that register as `SPEAKER`, with `ON` and `OFF`, which is the mute. It
is a separate command from `SOUND` on purpose: a sound command that enabled the speaker on
its way past would leave `SPEAKER OFF` unable to hold, so nothing but `SPEAKER` writes
this register. A sign found silent is a sign whose register is `FF`, and that is the first
thing to check when `SOUND` appears to do nothing.

## What the spike still has to confirm

The wire format questions are closed. Four behavioural ones were open, and a session with
the sign on 2026-09-09 settled three of them. What each turned out to be is recorded here
rather than deleted, because the next person will want to know it was answered on hardware
and not merely assumed.

1. **Is the rotation seamless?** Answered yes, near enough. Files A, B and C cycling by
   themselves ran without much of a pause, so server-side rotation is not needed.
2. **Does rewriting only the run sequence disturb the display?** Answered no. A run
   sequence written while the rotation was on screen left it running, with no blank and no
   restart. So a slot expiring by TTL, which rewrites the sequence, costs nothing visible.
3. **Does a run sequence write cancel a running priority message?** Still open. The
   session did not produce a clean test of it, so the service keeps the cautious reading
   below and defers run sequence writes while an alert is up.
4. **Does the sign answer reads through the Ethernet adapter?** Answered yes. All four
   reads in the table below came back correct, so the adapter is two-way and divergence
   could be detected by asking rather than by re-pushing on a timer.

The same session turned up a fifth thing that was not on this list, because nobody thought
to doubt it: a memory configuration does not display unless a bare `E$` clear precedes it.
See "A clear must come first" above. That was the bug behind a sign that accepted every
write and showed nothing.

And one measurement remains: the inter-packet delay this sign actually needs. The old
implementation slept two seconds after every write and closed the port; that number was
never measured, and `inter_packet_delay` defaults to a conservative 0.5s until it is.

`scripts/protocol_spike.py` also re-proves the memory configuration, the run sequence and
the priority takeover end to end, which is cheap and worth doing since it is already
standing in front of the sign. It is destructive, because step 2 erases the sign, so it
refuses to run without `--confirm-erase`.

## Things that cancel a running priority message

The document lists exactly four, and one of them is the intended release path:

- a bare priority write, `A0` with nothing after the label, which is how an alert is
  released;
- **any serial write to the Run Time table**;
- **any serial write to the Run Day table**;
- the PROG key on an infrared keyboard.

The two in bold matter because they are things a service could plausibly do while an
alert is up. This one does not touch either table, so it is safe on that count.

What the document does **not** say either way is whether a **Set Run Sequence** write
disturbs a running priority message. That is not an academic question: a slot expiring by
TTL rewrites the run sequence, and if that cancels the alert then an alert would vanish
mid-display for reasons nobody watching could explain.

Until the spike answers it, the service takes the cautious reading. While an alert is
active the registry holds run sequence writes back and applies them when the sign is
handed back. Writing a slot's own TEXT file is not on the list above and carries on
normally, so content stays current behind the alert.

If the spike shows a run sequence write is harmless during an alert, the deferral can be
dropped and `MessageRegistry._apply_run_sequence` becomes simpler.

## Reading state back

The `F` command code reads a special function, so there is a read for each thing this
service writes:

| Payload | Returns |
| --- | --- |
| `F$` | the whole `FTPSIZEQQQQ` memory configuration table |
| `F#` | the memory pool's total and unused size |
| `F.` | the current run sequence |
| `F)` | the run time table, including whether a priority message is running |

These would turn divergence detection from a timer into a question. The service currently
re-pushes everything every fifteen minutes, because the sign and the Ethernet adapter are
separately powered: the sign can be power cycled with the TCP link still up, nothing
fires, and the suppression cache then skips exactly the writes that would repair a blank
sign. Being able to ask would replace that with a cheap comparison.

The frame builders exist, and the adapter is two-way: this sign answered all four of these
reads through it on 2026-09-09, and answered them again during the soft reset check above,
which read two text files back with `B` as well. Nothing in the service depends on that
yet, which is deliberate; the reads are available whenever divergence detection is worth
building.

One trap when reading: a reply opens with a run of `NUL`s and the payload arrives a moment
behind the first byte, so a reader that takes `in_waiting or 1` and stops returns a lone
`b"\x00"` for every question. Compare two of those and they match, which looks like proof
and is not. Drain until the sign goes quiet instead. `scripts/protocol_spike.py` has the
eager version. Nothing in the service depends on the answer
yet, which is deliberate.

One trap when comparing a read-back memory configuration against a plan: the sign gives
whatever is left of the memory pool to the **first** file in the configuration once it
starts running. The first file's size will therefore never match what was sent. Compare
the plan semantically, not byte for byte.

## Constraints the frame builders honour

**Protocol generation.** The compatibility matrix lists the BetaBrite as EZ KEY II and
Alpha 1.0 only. Nothing marked Alpha 2.0 or 3.0 may be used, which rules out the `E$$$$`
clear-memory-and-compact-flash command, programmable sounds, and the ACK/NAK response
feature. `constants.PROTOCOL_GENERATION` records this.

**File labels.** Valid labels are any printable ASCII from 0x20 to 0x7E, and a run
sequence holds up to 128 of them, so the real ceiling on how many messages can share the
sign is the memory pool in bytes rather than a count of files. This service allocates `A`
through `Z` anyway, because a label a person can read in a log line is worth more than
the extra capacity. Two ranges are avoided outright: `0`, which is the priority file,
and `1` through `5`, which become reserved target files if the sign's counter feature is
ever switched on.

**Timing.** The inter-byte timeout for a standard packet is one second, and a nested
packet needs at least 100 ms after its `STX`. This service sends no nested packets. The
`inter_packet_delay` setting defaults to a conservative 0.5s until the spike measures what
this sign actually needs.
