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

## What the spike still has to confirm

The wire format questions are closed. Four behavioural ones are not, and every one of
them needs the sign in front of you.

1. **Is the rotation seamless?** Several files playing in turn should not blank between
   them. If they do, the fallback is server-side rotation with a longer dwell time, and
   that is a decision to bring back rather than to take quietly.
2. **Does rewriting only the run sequence disturb the display?** A slot expiring by TTL
   does exactly that, every time, so a flicker here would be a recurring visible cost.
3. **Does a run sequence write cancel a running priority message?** See the section on
   what cancels a priority message. The service currently assumes it might.
4. **Does the sign answer reads through the Ethernet adapter?** See the section on
   reading state back. Nothing depends on the answer yet, but it decides whether
   reconciliation can stop being a timer.

And one measurement: the inter-packet delay this sign actually needs. The old
implementation slept two seconds after every write and closed the port; that number was
never measured, and `inter_packet_delay` defaults to a conservative 0.5s until it is.

`scripts/protocol_spike.py` also re-proves the memory configuration, the run sequence and
the priority takeover end to end, which is cheap and worth doing since it is already
standing in front of the sign. It is destructive, because step 2 erases the sign, so it
refuses to run without `--confirm-erase`.

## Things that cancel a running priority message

The document lists exactly four, and one of them is the intended release path:

- an empty priority write, which is how an alert is released;
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

The frame builders exist. What is unproven is whether this sign answers a read **through
the Ethernet to RS-232 adapter** at all; two-way traffic over that path has never been
tried. The spike's step 5 tries all four. Nothing in the service depends on the answer
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
