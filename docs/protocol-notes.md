# Alpha protocol notes

What the Alpha Sign Communications Protocol actually says about the parts of it this
service depends on, with the quotation behind each claim.

**Status: confirmed against the protocol document, and much of it now confirmed on the
sign.** The wire formats below are quoted from the Alpha Sign Communications Protocol
itself, so they are no longer anybody's reading of anybody else's implementation. A session
with the real BetaBrite Classic at the end of an Ethernet to RS-232 adapter on 2026-09-09
settled three of the four behavioural questions that were open then. A second session on
2026-09-11 settled the fourth, answered a fifth that had been added in between, and took the
one measurement still outstanding. They are all listed at the end, with what each turned out
to be.

That session also answered several things nobody had thought to doubt, each recorded below
beside the measurement: a memory configuration does not display unless a bare `E$` clear
precedes it, releasing the priority file needs a bare write rather than an empty one, `E,`
restarts the sign without erasing it, the sign's date carries no century, its speaker is a
fixed-pitch buzzer, and twenty of the protocol's ways to draw text collapse into five on a
display seven pixels high.

`scripts/protocol_spike.py` re-proves the wire formats end to end. It is destructive, and
it refuses to run without `--confirm-erase`. `scripts/string_file_spike.py` does the same
for STRING files, which a session on 2026-09-10 measured; see "STRING files, measured on
the sign".

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
tried rather than at its edge. The wait is skipped only when `settle_delays_enabled` is
false, which is how the simulator and the test transport are run, because neither has a
reset to sit through. It used to be skipped when `inter_packet_delay` was zero, which
conflated how fast this end may talk with how long the sign is deaf; a real sign paced as
fast as the line allows takes just as long to come back.

### The erase is visible, and quick, measured on the sign

On 2026-09-11 the spike's step 2, a memory configuration write with no clear before it, was
watched three times over. The display does blank. It is brief, brief enough that the first
run through recorded no blank at all and the answer had to be corrected after two more
attempts. None of that is reassuring and none of it should be read that way: a memory
configuration write is destructive whether or not anybody catches the moment it happens.
The warning in AGENTS.md under "The one dangerous operation" stands exactly as written.

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

## Twenty ways to draw text, five of which look different

Control code 1AH selects a character set and 1DH switches a character attribute. Between
them the document offers fourteen character sets and six attributes, and the service
originally exposed none of either while offering three tokens of its own for overlapping
ideas: `<wide_on>` (12H), `<dbl_height_on>` (05H) and `<fixed_width>` (1EH).

On 2026-09-09 all of them were put on the real sign one at a time, held for eight seconds
each in a single colour, and photographed so that identical ones could be told from
nearly-identical ones. Twenty distinct codes collapsed into **five** appearances:

| Look | Code | Also identical to |
| --- | --- | --- |
| plain | none | see below |
| half height | `1AH+1` | |
| wide | `1AH+5` | `1AH+8` |
| bold | `1DH+0` | |
| extra wide | `1DH+1` | |

Everything else drew text pixel-identical to plain: character sets `1AH+2`, `+3`, `+4`,
`+6`, `+7`, `+9`, `+:`, `+;`, `+<`, `+=`, `+>`, the attributes `1DH+2` (double high),
`1DH+3` (true descenders) and `1DH+5` (fancy), and the protocol's own `12H` wide and `05H`
double height that two of the service's tokens were built on.

The reason is the display. A Betabrite is "always 7 dots (or pixels) high", and most of
these variants differ only in stroke weight or in height above seven rows. Seven rows
cannot express the difference between seven slim, seven stroke and seven fancy, and
nothing at all can be twice as tall as the whole sign. The sign accepts every one of these
codes and reports no error; it simply draws the same dots.

So `<wide_on>`, `<wide_off>`, `<dbl_height_on>` and `<dbl_height_off>` were removed, and
`<font_normal>`, `<font_half_height>`, `<font_wide>`, `<bold_on>`, `<bold_off>`,
`<extra_wide_on>` and `<extra_wide_off>` were added in their place. The three `font_`
tokens are a selection rather than a switch, which is why there is a `<font_normal>`: it is
the only way back from half height.

They are named for what a person sees, not for the document's labels, because those
contradict themselves on this hardware. The table calls `1AH+6` "ten high standard" and
also "seven stroke fancy" on a Betabrite, and on seven rows it is neither: it is ordinary
text. A name taken from that table would have described something nobody can see.

The constants for every one of these stay in `constants.py` with their citations, and the
sign simulator annotates them all, so a message written by an older version stays
readable.

### `<fixed_width>` survived, and nearly did not

The third of the service's own tokens, `<fixed_width>` (1EH+1), was very nearly removed
with the other two, and the reason it was not is worth keeping.

Every sample above was the string `Ag8`, which is a fine probe for a glyph shape and
useless for a spacing one: fixed width means every character takes the same cell, so
seeing it needs characters whose natural widths differ. `Ag8` has no narrow letter to
compare against a wide one, and against that sample 1EH+1 looked exactly like plain text,
which is precisely what a dead token looks like.

Re-run with `iiiiiWWWWW`, the widest contrast the ASCII set offers, the difference was
immediate: the inter-character spacing changes dramatically and the five `i`s take the
room the five `W`s do. The token works as documented and stays.

The lesson generalises to anything measured on this sign. A sample has to be able to show
the thing being looked for, and "it looked the same" is only evidence when it could have
looked different. Four tokens were removed on that kind of evidence above; this one shows
how close that reasoning came to removing a fifth that works.

## The extended character set ends at C1H, whatever the table says

Control code 08H plus an offset, or a single byte from 80H, reaches a second character
table beyond ASCII: accented letters, currency marks and a few symbols. The document
tabulates it three columns wide, a code, the character, and the control code combination,
across pages 84 to 87.

The middle column is the problem. It is drawn as vector outlines rather than set as text,
so it survives neither extraction nor a search, and for a long time the names in
`constants.py` were the one part of that module with no citation behind them. On
2026-09-10 the three pages were read as images, which is exactly what
`tests/test_constant_values.py` had been asking for in the docstring of the test that
recorded the gap.

### What the reading changed

**9EH is the peseta sign**, a `Pt` ligature, and had been called `PERCENT` on a guess.
Nothing depended on the name, so the rename was free.

**Codes 80H to A8H are IBM CP437 exactly**, all forty-one of them. That is independent
corroboration of every identity in that run, arrived at from a different direction than
the scan. It is also the boundary of the corroboration: CP437 puts a reversed-not sign at
A9H and this table has a degree sign there, which the sign has been drawing after the
outdoor temperature for years. So the agreement is a stretch of the range and not the
whole of it, and filling any later gap from CP437 would be wrong.

**7EH is a half space, not a tilde.** Table 33 on page 50 annotates it `1/2 sp` and closes
with the note `1/2 sp = 1/2 space`. The `<half_space>` token was right and the `TILDE`
constant under it was not; it is `HALF_SPACE` now.

### Twenty-four characters were unreachable, and ten now are not

The table holds sixty-six characters and `markup.py` mapped forty-two of them. The other
twenty-four had no route in at all: no token, no Unicode mapping, nothing. The service
answered a message containing `Á` with "the sign cannot display 'Á'", which is prose the
service emits, and it was false.

Ten were added, the ones whose glyph is unambiguous at the scan's resolution: `₧`, `ƒ`,
`ª`, `º`, `θ`, `Θ`, a single column space, and the accented capitals `Á`, `Ê` and `Í`.
Each was then drawn on the sign beside the character it is mapped from before the mapping
was written, because a mapping asserts an identity and a scan read wrongly would put a
silently different glyph on the display.

The remaining fourteen stay out on purpose. Codes B0H to B9H look like a Croatian or
Serbian set, and the diacritics at BBH to BDH cannot be told apart at five dots by seven.
Guessing one would be worse than leaving it out: an unmapped character is refused with a
message saying so, while a wrongly mapped one is accepted and drawn.

### The twenty-four pictographs the document promises and the sign does not have

This is the part worth knowing before anybody "fixes" the range.

The running header reads `Extended character set (80 - C1H)` over pages whose table
carries twenty-four further rows, C2H through D9H. They are pictographs, and their
characters are printed as words rather than drawn, which is why they survive extraction
when nothing else in that column does: an Euro symbol, four arrows, then Packman,
Sailboat, Ball, Telephone, Heart, Car, Handicap, Rhino, Mug, Satellite dish, Copyright,
Male, Female, Bottle, Diskette, Printer, Musical note and Infinity.

Footnote 1 against them reads: "Only applies to Betabrite 1036, Alpha Premiere 9000, and
AlphaEclipse signs." That names this sign first, and every other exclusion in the document
runs the other way, so the reasonable reading was that `constants.py` stopping at C1H was
an artifact of the contradictory header.

It is not. All twenty-four went to the sign on 2026-09-10, in both documented encodings,
the bare byte and 08H plus an offset, with the degree sign sent first on each pass as a
positive control. The control drew correctly both times. Every one of the twenty-four came
back as a question mark, which is the sign's own unknown-character glyph rather than
anything this service substituted: the test wrote the raw byte between brackets and read
back `C4[?]`.

So the header is right, the footnote is wrong, and the ceiling in `constants.py` is
correct as it stands. This is the fourth thing the document promises that this hardware
does not do, after double height, the wide character set, and the programmable tone's
frequency byte.

`tests/test_constant_values.py::test_the_pictographs_past_the_range_are_absent_on_purpose`
fails if anybody adds one, because the absence looks exactly like an oversight and the
obvious repair is to read the table to its end.

One incidental finding is worth keeping. The sign substitutes `?` for a character it does
not have, which happens to be the byte `markup.py` already uses as `REPLACEMENT` on its
lenient path. The service and the hardware agree on what an unrenderable character looks
like, by coincidence rather than by design.

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

## STRING files, measured on the sign

A STRING file is a small file that a TEXT file calls inline with 10H followed by the
STRING's label. It is written with `G` (47H) and read with `H` (48H). The document's case
for it is the one this service has: "applications where a string of frequently changing
data must be transmitted to, and displayed by, a sign", and "When writing STRING files to a
message center, the display will not blank as it does when writing TEXT files. This is
because the STRING file data is buffered and TEXT file internal Checksum does not change.
Because the STRING file data is buffered, the size of a STRING file is limited to 125
bytes."

It has to be allocated in the memory configuration first, as type `B`, and Table 15 adds
two rules for that entry: "For a STRING file, "L" must be selected", and "For a STRING
file, use "0000" as place holders". Appendix A adds that "File Label "0" (30H) and "?"
(3FH) can not be used as STRING file labels." Table 18 then lists what a STRING may hold:
20H to 7FH and eleven control codes, with the note "Rainbow 1 and 2 colors do not work in
STRING files".

`scripts/string_file_spike.py` put all of that to the sign on 2026-09-10, beside a TEXT
file calling each STRING. What it found:

| Question | What the sign did |
| --- | --- |
| Are `a` to `z` files of their own, apart from `A` to `Z`? | Yes. STRING `a` beside TEXT `A` drew `UP:low`, and for the rest of the session neither disturbed the other. |
| Does a STRING write blank the display? | No. Three TEXT rewrites blinked each time; six STRING writes changed the number with no blink. |
| And in the middle of a ROTATE scroll? | No restart, jump or blank either. The new value is not drawn mid-pass: it appears on the next pass. |
| Does a changing value move the text around it? | Yes, with proportional spacing, as Appendix D warns. With `<fixed_width>` in the TEXT file before the call the text stayed put, and the line was left justified rather than centred, which is what that token already says it does. |
| A degree sign in the TEXT file after the call? | `72°F`, drawn correctly. |
| The control codes Table 18 allows | All worked: colour, two colours in one value, half height, fixed width, the time, a new line and the speeds. |
| The codes Table 18 leaves out | Most worked anyway: rainbow 1, colour mix, flash, bold (1DH), the degree sign in both its 08H 49H and A9H forms, É at 90H, and a new page. Two failed. The day of week (0BH 39H) drew a literal `9`, and a STRING calling another STRING drew the label as a letter, `XbX`. |
| Does formatting set in a STRING stay in it? | No. A colour, the half height character set and a speed set inside a STRING all carried on into the TEXT file after the call. |
| What does a call to nothing draw? | Nothing at all, not even a space, whether the STRING was allocated and never written, never allocated, or the label was a TEXT file's. |
| A STRING written past its size | Emptied. Twenty-four bytes into a 16-byte STRING and 130 into a 125-byte one each left the call drawing nothing. Not truncated and not refused: the previous value was lost too. |
| Can the priority file call a STRING? | Yes. An alert calling one drew it, a STRING write during the alert changed the number inside it, and the alert kept the sign throughout. The bare release brought the rotation back as usual. |
| What does a read return? | What Table 20 says: `G`, the label, the data, ETX and a checksum, which summed correctly by hand (`027B` for `GaREAD ME`). The display blanked briefly mid-scroll and picked up from about where it was. A read of a label never allocated came back as `Gz` with no data. No empty STRING was read, but that reply has nowhere to say "not allocated", so presumably the two look the same. |
| Does a STRING survive a power cycle? | A short one, yes, and so did the TEXT file calling it: `PC:42` came back. Not one to rely on, though. From experience with this sign, its memory lasts through about five minutes unplugged and is gone after a day or more, and the point between has not been pinned down. So the service goes on assuming a power cut can wipe the sign, which is what the periodic refresh repairs. |

One sample was tried from each family of codes rather than every token: one character set
of the three, one 1DH attribute of the two, two speeds of five, two of the extended
character forms, rainbow 1 but not rainbow 2 or automatic colour, and none of `<block>`,
`<half_space>` or `<no_hold_speed>`. The rule below is an inference by family from those.

The spike looked for each STRING's entry in the `F$` memory configuration reply and found
none, although the files plainly existed. The reply explains it, and the fault was the
reader's, not the sign's:

```
<- 49 bytes: b'\x01000\x02E$AAU0100FFFFBAU00'
```

That stops five characters into the second entry, with no ETX, checksum or EOT after it.
This was first put down to the sign pausing mid-reply for longer than the spike's 200 ms
quiet rule, and that was wrong. The reader took one byte a poll, for the reason given under
"Reading state back", and ran out of time with the reply half collected. So how the sign
lists a STRING entry is still unmeasured.

And at speed 5 the person watching counted eight repetitions of a word sent five times,
which is more likely a count lost at that speed than the sign repeating anything.

What this settles for the service:

- **Table 18's list is not this sign's list.** The document says rainbow does not work in a
  STRING, and it does. What a STRING cannot hold is narrower than the document says: the
  date inserts (0BH), and another STRING call. Every family of code the message markup
  offers that was tried worked. The service's rule for a value is therefore the message
  markup less those two, not the document's list.
- **Formatting in a value leaks.** A value of `<red>DOWN` turns the rest of the message red
  too. A TEXT file that cares what colour follows a call has to set it again after the
  call.
- **The size check is the service's job, and it is not optional.** An overrun does not cut
  the value short, it destroys it.
- **A dangling call is invisible.** A call whose STRING has gone draws nothing, not garbage,
  and a freshly allocated STRING needs no blanking before it is used.
- **Alerts can carry live values**, and a STRING write can go out while an alert is up. It
  is not on the list of things that cancel one, and on this sign it did not. That rests on
  one alert calling one STRING, which is all the session tried.
- **Reading a STRING back has no place in normal running.** It blanks the display, and it
  cannot tell an unallocated label from an empty one.

## What the spike still has to confirm

The wire format questions are closed, and as of 2026-09-11 so are all five behavioural ones.
A session on 2026-09-09 settled three of the four open then, and a second on 2026-09-11
settled the fourth along with a fifth added in between. What each turned out to be is
recorded here rather than deleted, because the next person will want to know it was answered
on hardware and not merely assumed.

1. **Is the rotation seamless?** Answered yes, near enough. Files A, B and C cycling by
   themselves ran without much of a pause, so server-side rotation is not needed.
2. **Does rewriting only the run sequence disturb the display?** Answered no. A run
   sequence written while the rotation was on screen left it running, with no blank and no
   restart. So a slot expiring by TTL, which rewrites the sequence, costs nothing visible.
3. **Does a run sequence write cancel a running priority message?** Answered no, on
   2026-09-11. With ALERT holding the whole display, the run sequence was rewritten from
   A B C to A B, a real change rather than a no-op, and the alert stayed up. The service
   deferred run sequence writes while an alert was active until this settled it, and no
   longer does; see below.
4. **Does the sign answer reads through the Ethernet adapter?** Answered yes. All four
   reads in the table below came back correct, so the adapter is two-way and divergence
   could be detected by asking rather than by re-pushing on a timer.
5. **What does an empty run sequence show?** Answered on 2026-09-11: **the sign freezes on
   the message it was showing**, ONE in that run, and holds it for as long as the sequence
   names nothing. It does not blank, and it falls back to nothing of its own. That matters
   twice. `DELETE /messages` empties the sequence and then blanks each file, and the
   blanking is load-bearing rather than tidiness: without it the last message would sit on
   the display indefinitely. And a message deactivated rather than deleted would do the same
   if it were the last one active, so whatever implements that has to blank the file when
   the sequence empties. Naming the three files again brought the rotation straight back,
   every message's text and colour intact, with nothing rewritten in between: reactivating
   costs one packet and no redraw.

The same session turned up another thing that was not on this list, because nobody thought
to doubt it: a memory configuration does not display unless a bare `E$` clear precedes it.
See "A clear must come first" above. That was the bug behind a sign that accepted every
write and showed nothing.

The 2026-09-11 session turned up one of its own, from reading the run sequence back while it
was empty. The write was `E.SU`, the ignore-time mode this service always sends, and the
sign answered `E.TU`: the mode byte came back as `T`, run each file by its own times. That
is not the reply format flattening it, because the same read on 2026-09-09 came back
`E.SUABC` with the `S` intact. So an empty sequence appears to take the mode back to the
default, or the sign declines to store `S` when there are no labels to apply it to. Nothing
rests on it today, since every file this service allocates is always eligible and the two
modes behave identically for it, but a file that ever gains a real schedule would want this
settled first.

The one outstanding measurement, the inter-packet delay this sign actually needs, was taken
on 2026-09-11. Six writes in a row landed correctly at gaps of 1s, 0.5s and 0.25s, and
failed at 0.1s. So this sign is good to at least 0.25s and its floor is somewhere between
0.1s and 0.25s. `inter_packet_delay` still defaults to 0.5s, which is now a conservative
choice with a measurement behind it rather than a guess, and 0.25s is there for anyone who
wants the sign to keep up with a burst. The old implementation slept two seconds after every
write and closed the port; that number was never measured at all.

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
disturbs a running priority message. That was not an academic question: a slot expiring by
TTL rewrites the run sequence, and if that cancelled the alert then an alert would vanish
mid-display for reasons nobody watching could explain.

The spike answered it on 2026-09-11. With an alert holding the display, the run sequence was
rewritten from A B C to A B, and the alert stayed up. So a Set Run Sequence write is not a
fifth thing that cancels a priority message, and the list above is the whole list.

Until that measurement the service took the cautious reading: while an alert was active the
registry held run sequence writes back and applied them when the sign was handed back. That
is gone, and it took with it `MessageRegistry._apply_run_sequence`'s `force` flag,
`flush_deferred`, the alert service's release hook and the simulator's warning about it.
Writing a slot's own TEXT file was never on the list above and was never held back, so
content stayed current behind the alert either way.

## Reading state back

The `F` command code reads a special function, so there is a read for each thing this
service writes:

| Payload | Returns |
| --- | --- |
| `F$` | the whole `FTPSIZEQQQQ` memory configuration table |
| `F#` | the memory pool's total and unused size |
| `F.` | the current run sequence |
| `F)` | the run time table, including whether a priority message is running |
| `F"` | general information, described below |

`F"` is the one worth knowing about, and `GET /sign/information` now sends it. Table 16 gives
its reply as `FFFFFFFFfMmYyHhNnRSSPOOL,pool`: eight characters of firmware version, a
revision letter, the firmware's release month and year, the sign's clock, the time format,
the speaker status, and the memory pool's total and unused size. The document's own note on
it is "General Information is most useful as a source of troubleshooting information", and
it answers in one read most of what the four above answer separately.

It is also the only way to ask this sign what it is, which is an open question rather than
an idle one: the extended character table's footnote claims a Betabrite 1036 draws the
pictographs at C2H to D9H, and this sign draws none of them.

Two things about the reply are worth knowing before touching `readerboard/protocol/replies.py`.
The sign echoes the **write** command code `E` in its answer to a read, not the `F` that was
sent, which reads like a mistake in the document and is what it specifies. And the length is
"28 or 29 ASCII characters", the difference being the firmware revision letter, which sits in
the middle: read the fields left to right at fixed offsets and a sign that omits it shifts
every field after it into a plausible wrong answer. The parser measures from both ends
instead.

Collecting the reply has its own traps, described under "Reading state back" and encoded in
`tests/test_controller.py` and `tests/test_transport.py`: the answer is read until its EOT
arrives, and what is waiting is drained rather than read once.

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

The trap when reading is pyserial's, not the sign's. Over `socket://`, which is how this
sign has always been reached, `in_waiting` is not a byte count: it is 1 while anything is
waiting and 0 when nothing is. So `read(in_waiting)` takes one byte however much has
arrived, and that went wrong three ways before it was found:

- A reader that waited two seconds and then read `in_waiting or 1` once got a lone
  `b"\x00"`, the first of the run of `NUL`s every reply opens with, for every question.
  Compare two of those and they match, which looks like proof and is not. This was
  first explained as the payload arriving a moment behind the nulls, and nothing
  measured says it does.
- A reader polling every 50 ms collected one byte per poll, twenty a second, and a
  three second deadline cut every reply longer than about fifty bytes off partway
  through. The memory configuration read in "STRING files, measured on the sign" came
  back that way. This was first explained as the sign pausing mid-reply, which is also
  unmeasured.
- In 0.5.0, where a reply that has not finished by the deadline is a 503 rather than
  half an answer, `GET /sign/information` failed through the adapter every time.

So drain what is waiting, reading until `in_waiting` says nothing is left, and keep
reading until the EOT that closes every transmission, with a deadline for a sign that
never sends it. A serial port reports the real count and drains in one pass. A reader that
stops when the line goes quiet is still wrong, even though no pause has been measured,
because a reply that stops early can still parse: half a memory configuration is a shorter
configuration.

One trap when comparing a read-back memory configuration against a plan: the sign gives
whatever is left of the memory pool to the **first** file in the configuration once it
starts running. The first file's size will therefore never match what was sent. Compare
the plan semantically, not byte for byte.

## What this sign cannot do, audited against the whole document

On 2026-09-10 the document was swept end to end for anything the service does not offer,
because two earlier findings had shown that reading it piecemeal misses things: the
pictographs at C2H to D9H were hidden behind a section header that contradicted its own
table, and the twenty character sets and attributes had never been compared against the
display's seven rows.

The sweep covered Table 15 (Write SPECIAL FUNCTION), Table 16 (Read SPECIAL FUNCTION),
Tables 65 to 67 (modes), the display position field, the whole control code table in
Appendix G, and the appendix index. What follows is the result, so that the next person
asking "did we miss a feature?" can read it rather than derive it again.

### One mode the document does not describe at all

Table 65 has twenty-two rows and twenty-one of them carry a name and a
description. The row for `d` (64H) carries the word "reserved" and nothing else.

On this sign it is a mode. Held for thirty seconds on 2026-09-10, against `AUTO`
and against `HOLD` in the same run, it drew the message with a random transition
and a random colour. `AUTO` shuffles the transition only, so the colour is what
separates them, and nothing else in the table randomises colour. It is offered as
`AUTO_COLOR`.

The colour randomisation overrides the message. Every sample in that run was
written with an explicit green in front of it, `1CH 32H`, and the sign drew it in
changing colours anyway.

This is the fifth thing the document has got wrong about this hardware, and the
first where it was wrong by omission rather than by promising too much. The other
four all over-promised: double height, the wide character set, the programmable
tone's frequency byte, and the four text positions. An empty row turns out to be
a stronger reason to look than a row that says no.

`tests/test_constant_values.py::test_the_mode_the_document_calls_reserved` pins
the byte, and says in its docstring that it is the one value in that file with no
citation behind it, because the document has none to give.

### The gap between the two special mode tables is empty, and was checked

Table 66 runs its specifiers from `0` to `C` and Table 67 begins at `S`, skipping `T`.
Nothing in the document says what, if anything, lives in between. After 64H turned out to
be a real mode hiding under the word "reserved", that gap looked worth a sweep.

It is empty. All sixteen codes, `nD` through `nR` and `nT`, went to the sign on 2026-09-10
and every one of them drew INTERLOCK.

Two things follow, and the second is the more useful.

The sweep is done and does not need repeating. This is recorded as a negative result on
purpose: "nobody has looked" and "somebody looked and there is nothing there" are different
states, and only one of them is worth spending four minutes of attention on again.

**An unrecognised special mode specifier is not refused, it falls back to INTERLOCK.** The
sign accepted every one of those writes and displayed the message; it simply drew it in a
mode nobody asked for. So a message that interlocks for no apparent reason is worth
suspecting a bad specifier byte, and the sign will never say so itself. Why INTERLOCK is
the fallback rather than the first entry in the table is not known and the document does
not say.

### Modes and positions are complete

Table 65 has twenty-two standard mode codes and every one is accounted for. `d` (64H) is
the mode the document calls reserved and this sign draws anyway, described above. `n` (6EH) is the SPECIAL prefix, which the special modes below are reached
through. `m` (6DH) SCROLL is "New message line pushes the bottom line to the top line **if
2-line sign**". `u` (75H) EXPLODE and `v` (76H) CLOCK are both marked Alpha 3.0, and Table
3 gives a Betabrite as EZ KEY II and Alpha 1.0 only. The remaining seventeen are all
offered.

Table 66's thirteen special modes are all offered but one: `C` (43H) CYCLE COLORS, whose
footnote reads "COLOR CYCLE will only work on AlphaEclipse 3600 signs". All seven of Table
67's special graphics are offered.

The display position field has six values. The four the service offers are `20H` Middle,
`22H` Top, `26H` Bottom and `30H` Fill; `31H` Left and `32H` Right are Alpha 3.0 only.

None of the four is offered any more. The note closing that list reads: "On one-line
signs, the Display Position is irrelevant", a Betabrite is one line, and the sign
confirmed it on 2026-09-10: all four drew the same thing. So they went the way
`<wide_on>` and `<dbl_height_on>` went, and for the same reason. A name for a distinction
nobody can see is a promise the sign does not keep.

The byte did not go anywhere, because it cannot. The document is explicit that "Display
Position is irrelevant, but it still must be included", so `frames.write_text_file` sends
`TEXT_POS_MIDDLE` on every write and takes no parameter for it. All four constants stay in
`constants.py` with their citations, and the sign simulator still names all four, because
it decodes what arrives on the wire rather than what this service chose to send.

A caller that still sends a position is refused with a 422 rather than quietly having its
choice dropped, because the request models forbid unknown fields. That is deliberate: a
silent success would leave somebody believing the sign had honoured it.

### Excluded because the document says so

Each of these exists in the protocol, is absent here, and names the reason.

| Feature | Why not this sign |
| --- | --- |
| The whole of Appendix M | Headed "Alpha 2.0 protocol additions", and "the Alpha 2.0 protocol is only available for the AlphaPremiere and AlphaEclipse signs" |
| Set Dimming Register (2FH) | "Dimming is only available on Solar signs" |
| Set Dimming Times (2FH) | "Dimming times is only available AlphaEclipse signs" |
| Enable/Disable ACK/NAK (73H) | Alpha 2.0 and 3.0 only |
| Display Text at XY Position (2BH) | ALPHAVISION character matrix signs |
| Set Counter (35H), counter inserts (08H+7AH to 7EH) | "the five internal timers available on counter-equipped signs" |
| Set Color Correction (43H, 33H, 58H) | Alpha 3.0, AlphaEclipse 3600 RGB signs |
| The Set Unit family (31H to 39H, 4EH) | AlphaEclipse tiled displays |
| Set Temperature Offset (54H), temperature inserts (08H+1CH, 1DH) | "only on Solar, 790i, 460i, 440i, and 430i" |
| Auxiliary Port attribute (1DH+6) | "Series 4000 & 7000 signs only" |
| Speed control (0FH) | Alpha 2.0 only |
| Clear Memory and Compact Flash (four 24H) | Alpha 3.0 only |
| LF (0AH) and 0EH | No meaning at all: both rows of the control code table are blank |

Appendix M is the one worth reading twice, because its subsection list is the most
tempting thing in the document. It contains a Dimming Control Register, custom character
sets, an automode table that chooses which modes AUTO draws from, a timeout message and
sound control. None of it reaches this sign, and one sentence at the head of the appendix
covers all of it.

Two exclusions were settled on the hardware rather than from the document, and are written
up in their own sections above: the pictographs at C2H to D9H, which the document says a
Betabrite 1036 draws and this sign renders as question marks, and the Shadow attribute
(1DH+7), which the document restricts to "Betabrite model 1036 and AlphaPremiere 9000
signs only" and which drew as ordinary text in the font parade.

The absence of ACK/NAK is worth one extra line, because it explains a design decision
elsewhere. This sign cannot acknowledge a write. That is why the service re-pushes
everything on a timer rather than trusting that a write landed, and why the read commands
above are the only way to ask.

### Capabilities that do apply and are not used

These are not exclusions. The document offers them, nothing says this sign lacks them, and
the service simply does not use them. They are recorded so that "we never thought of it"
and "we thought about it" stay distinguishable.

STRING files were on this list until they were measured on the sign. They have a section
of their own above, "STRING files, measured on the sign".

**SMALL DOTS PICTURE files** (`I` and `J`, called with 14H, or by name with 1FH). Bitmaps
up to 31 by 255 pixels that "can be used to create virtually any logo pattern on the
display of the sign", stored as their own file type and inserted into a TEXT file. On a
seven-high display that is a 7 by N bitmap, and it is the way to draw an arrow, a heart or
a musical note now that the pictograph range has turned out to be absent.

**Read General Information** (`F"`). See "Reading state back" above.

**Run Time Table** (29H) and **Run Day Table** (32H). Per-file start and stop times, and
per-file start and stop days including `0` Daily and `8` Monday-Friday. The service writes
`FFFF`, always, and expires slots host-side on a TTL instead. Note that both tables are on
the short list of things that cancel a running priority message, so scheduling and alerts
interact.

### One thing this audit found wrong rather than missing

The tone command carries two footnotes that the implementation does not honour.

> **2** the tone generation command must be the last transmission frame because the sign's
> serial port is disabled (and cannot receive any data) while a tone is generated.

> **4** Wait a minimum of 3 seconds before transmitting more data to the sign.

`SOUND` did not settle. A soft reset was the only command that asked for a wait, so after
a tone the next write went out once `inter_packet_delay` had passed, which defaults to half
a second. A write inside that window reached a sign whose serial port was off and was lost,
and the controller's suppression cache then believed it had succeeded, so nothing retried
it until the next periodic re-push.

This is fixed. `SOUND` now asks for `SOUND_SETTLE_SECONDS`, three, and the controller holds
the sign's lock across the send and the wait, so another writer queues rather than writing
into the gap. The wait is governed by `settle_delays_enabled` alone, so pacing the line
differently cannot take it away.

The property that made the fix worth generalising is that a tone is not a reset and deafens
the sign anyway, so `resets_the_sign` became `quiet_seconds_after`: it asks how long the
sign cannot listen rather than why. The regression test drives a write at a controller that
is mid-tone and asserts the write does not complete, which is a test that could have shown
the failure; asserting that the settle map contains three seconds would only have compared
the map to itself.

## Constraints the frame builders honour

**Protocol generation.** The compatibility matrix lists the BetaBrite as EZ KEY II and
Alpha 1.0 only. Nothing marked Alpha 2.0 or 3.0 may be used, which rules out the `E$$$$`
clear-memory-and-compact-flash command, programmable sounds, and the ACK/NAK response
feature. `constants.PROTOCOL_GENERATION` records this.

**File labels.** Valid labels are any printable ASCII from 0x20 to 0x7E, and a run
sequence holds up to 128 of them, so the real ceiling on how many messages can share the
sign is the memory pool in bytes rather than a count of files. This service allocates `A`
through `Z` for TEXT files and `a` through `z` for STRING files anyway, because a label a
person can read in a log line is worth more than the extra capacity. That the two cases
are different files was measured, not read; see "STRING files, measured on the sign".
Two ranges are avoided outright: `0`, which is the priority file, and `1` through `5`,
which become reserved target files if the sign's counter feature is ever switched on.
Appendix A rules `?` out for a STRING file as well.

**Timing.** The inter-byte timeout for a standard packet is one second, and a nested
packet needs at least 100 ms after its `STX`. This service sends no nested packets. The
`inter_packet_delay` setting defaults to 0.5s. This sign was measured on 2026-09-11 taking
six writes in a row correctly at a 0.25s gap and failing at 0.1s, so the default has margin
over what the hardware needs.
