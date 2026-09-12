# Changelog

Notable changes to readerboard. Versions follow [semantic
versioning](https://semver.org/spec/v2.0.0.html): while the major version is 0
the interface may still change, and any such change is called out here under
**Changed** or **Removed** rather than assumed to be obvious from the version
number.

What is versioned is the HTTP surface: the paths, the request and response
bodies, the status codes, and the settings names. The `readerboard` package is
importable and its modules are documented, but it is a service rather than a
library, and the names inside it may move without that being a breaking change.

## [Unreleased]

### Added

- **A message can be hidden without being given up.** `PUT /messages/{key}/active`
  with `{"active": false}` takes a message off the display and leaves it
  registered, keeping its slot, its file, its place in the order and its text, so
  `{"active": true}` shows it again and needs no copy of what it said. The run
  sequence names the active slots and nothing else, which is the whole mechanism,
  and the messages still showing carry on without a blank or a restart. A hidden
  slot still counts against `slot_count`, since it is still holding a file.

  Switching one either way is a single run sequence write while any other message
  is playing. The hidden file keeps its text, so there is nothing to send back
  when it is shown again, and the sign was measured taking a sequence write with
  the rotation on screen without a blink. The exception is the last message on
  the sign: hiding that one empties its file as well, because a sign whose run
  sequence names nothing freezes on whatever it was drawing and holds it, so the
  display would otherwise never clear. Coming back from there costs the text and
  the sequence both.

  `active` is also a field on `PUT /messages/{key}`, where it is optional and
  three-valued. Left out, it leaves the message showing or hidden exactly as it
  found it, so a source re-sending the same content every few minutes cannot
  switch back on something that was deliberately hidden. Sent, it moves the
  message, which is what makes a recurring notification one call: something that
  should appear for a minute whenever an event happens sends the text, a
  `ttl_seconds`, `delete_on_expiry` false and `active` true, and sends the same
  shape again at the next event. Without it that is two calls, because a
  deadline that hides a message clears itself on the way out.

  `PUT /messages/{key}/active` stays, for hiding or showing something without
  resending a message the caller may not have.

- **`delete_on_expiry` decides what a `ttl_seconds` does when it passes.** The
  default, `true`, is what a deadline has always done and hands the slot back.
  `false` hides the message and keeps the slot, for anything that comes back
  later rather than being finished with. An expiry that hides clears the deadline
  with it, so a message shown again does not vanish at the next sweep.

  A boolean rather than a word with two accepted spellings: the caller picks
  between the same two behaviours without having to find out which words the
  field takes, and a typo is a 422 naming the field rather than a value the
  service has to explain.

  Both fields appear in `GET /messages` and in the client, which grew the
  endpoint and a true/false field for each.

### Changed

- **Run sequence writes are no longer held back while an alert is up.** The
  protocol lists four things that cancel a running priority message and says
  nothing either way about a Set Run Sequence write, so the service had taken the
  cautious reading and held those writes until the alert was released. The sign
  settled it on 2026-09-11: with an alert holding the whole display the sequence
  was rewritten from three files to two and the alert stayed up. So they go out
  as they are made. Nothing about the HTTP surface changes; what changes is that
  a slot registered, expired or hidden during an alert reaches the sign then
  rather than at the release.

- **`inter_packet_delay` now defaults to 0.25 seconds rather than 0.5.** The old
  figure was a guess made before anyone had asked the sign. A BetaBrite Classic
  took six writes in a row correctly at a 0.25 second gap on 2026-09-11, and the
  same run failed at 0.1, so the new default sits above the measured floor and
  makes a burst of writes land in half the time. It is still a setting: a sign
  that needs more can be given more, and the symptom of too little is writes
  going quietly missing rather than an error, since a write the sign is too busy
  to hear is accepted by the link and never refused.

## [0.5.1] - 2026-09-11

**A fix for signs reached through an Ethernet adapter.** `GET /sign/information`
now answers through one, where until now it could not. Nothing else changes, and
upgrading from 0.5.0 erases nothing.

### Fixed

- **`GET /sign/information` never worked through an Ethernet adapter.** Over a
  `socket://` address pyserial reports at most one byte as waiting, however many
  have arrived, and the service read exactly what it reported. So it took one
  byte every 50ms, and a reply the length of the general information ran out
  the three second deadline before its end. Before 0.5.0 the cut-off reply went
  to the parser, which could refuse it or read it wrong; since 0.5.0 it has been
  a 503 every time. What is waiting is now read until nothing is left, so a
  reply arrives as fast as the link carries it. A serial port was not affected.

  The 0.5.0 notes put the cut-off replies down to the sign pausing mid-reply.
  That was wrong: this was the cause, and no pause has been measured. Reading
  to the EOT is still how a reply is collected.

## [0.5.0] - 2026-09-11

**This is the release that adds variables.** A variable is a value in a small file
of its own on the sign, called from a message or an alert with `<var:name>`, and
changing it neither blanks the sign nor restarts what calls it. Making room for
them changes the sign's memory configuration, so **the first start after upgrading
erases every message on the sign once**, and sources have to register theirs
again. Setting `variable_count = 0` before upgrading avoids that, with variables
switched off. A `ttl_seconds` also now ends within about a second of its deadline
rather than up to fifteen seconds late. **Read the Changed section before
upgrading.**

### Added

- **Variables: values that change without blanking the sign.** A variable is a value
  in a small file of its own on the sign, and a message calls it with `<var:name>`,
  as in `Outside <var:temp><degree>F`. Writing a new value rewrites only that file,
  which the sign takes without blanking or restarting the message calling it; a
  scrolling message shows the new value on its next pass. One variable can be
  called from any number of messages, and from an alert, which keeps the sign
  while the value inside it changes.

  `PUT /variables/{name}` creates or changes one, `GET /variables` and
  `GET /variables/{name}` read them back with the slots that call each and
  `called_by_alert`, and `DELETE /variables/{name}` deletes one. A name is one to
  32 lowercase letters, digits and underscores, so that every variable can be
  called from a message. A message or an alert calling a variable that does not
  exist is a 400. A variable that a message or the alert still calls cannot be
  deleted: that is a 409 naming what calls it, because the sign's file for it is
  written into each caller, and handing it to the next variable would put the
  wrong value on the sign.

  A value takes the message markup except `<week_day>` and `<var:name>`, which the
  sign draws as a literal character from inside a variable.
  `GET /enumerations/value-tokens` lists what is allowed, and
  `GET /enumerations/markup-tokens` now lists `<var:name>` as well. Formatting set
  in a value carries on into the message after the call. A value that does not fit
  its file is refused: the sign would empty it rather than cut it short.

  `ttl_seconds` and `stale_value` make a value go stale when it stops arriving: once
  the time passes the sign shows the stale value, such as `--`, until a new one is
  written. The variable itself stays, since messages call it.

  Two settings size the pool, `variable_count` (default 8, 0 turns variables off)
  and `variable_capacity` (default 32 bytes, at most 125), and both count against
  the same memory budget as the slots. `GET /health` reports `variables_used` and
  `variables_total`.

  All of this rests on a session with the sign rather than on the protocol
  document, which turned out to be wrong about what a STRING file can hold.
  `scripts/string_file_spike.py` is that session, and `docs/protocol-notes.md`
  records what it found under "STRING files, measured on the sign".
- **The sign simulator models STRING files.** It decodes a Write STRING, keeps each
  value, and shows every STRING file in the Files section. A message calling one
  reads with the value in place, as `{a: 72}`. It notes a value written past its
  file, which the sign empties, a call to a STRING file that is not there, which the
  sign draws as nothing, and a write to a label not allocated as a STRING file.
- **The client can call the variable endpoints**, with **Load keys** listing the
  variables and **Load From Sign** filling the value, the stale value and the time
  left before it goes stale. A value's **Insert token** offers the value tokens
  rather than the message tokens.

- **The client can load a stored message back into the form to edit it.**
  Register or replace a message now carries a **Load From Sign** button under the
  message caption. It calls `GET /messages/{key}` for the key in the box above
  and fills the form from what comes back: the message text with its markup, the
  display mode, the order and the source. Changing a message that is already on
  the sign no longer means retyping it out of the slot table.

  A key with nothing under it answers 404, and that answer is shown the way every
  other failure is. No field changes, so a half-written message survives a
  mistyped key.

  `ttl_seconds` is converted rather than copied, since the service takes a
  deadline as seconds from now and reports it as `expires_at`, the moment itself.
  The box gets the whole seconds remaining, so loading a message that expires and
  sending it back keeps roughly the deadline it had rather than dropping it. The
  clock runs while the form is open, so the deadline moves out by however long
  the edit took.

  It is left empty where there is no positive count of seconds ahead to show: no
  deadline, a timestamp that could not be read, and one already past. Empty is
  the field's own way of saying no deadline, and the response panel shows the
  timestamp regardless.

### Changed

- **The first start after upgrading erases the sign once.** The new default of eight
  variables changes the memory configuration, and writing a memory configuration
  erases every message on the sign, as changing `slot_count` always has. The
  service does it and says so at WARNING, and sources have to register their
  messages again. Setting `variable_count` to 0 before upgrading keeps the old
  configuration and avoids the erase, with variables switched off until it is
  raised.
- **A tag with a colon in it is now read as a tag.** `<var:name>` needs the colon,
  so a strict write of something like `<12:30>` is now refused as an unknown markup
  token rather than as an unterminated tag. It is refused either way, and a message
  restored from disk renders exactly as before.
- **Loading the slot keys in the client now clears a key that is not among
  them.** The key box keeps what was typed if the sign turned out to have that
  slot, and is emptied if it did not. The list is the answer to "what is
  registered", so a key missing from it is one no request on that form can
  succeed with: the read would 404 and so would the delete. It used to be kept
  regardless, which left a key that had just been shown not to exist sitting
  there looking exactly as valid as it did a moment earlier.

  The whitespace around a typed key is trimmed before it is compared, and on a
  match the box is rewritten to the trimmed key, so `  porch ` becomes `porch`.
  That is what the client sends anyway: it trims every path value on the way
  out, and the service refuses whitespace anywhere in a slot key. Case is not
  folded, because `Porch` and `porch` are different slots. Nothing is ever chosen
  for you: an empty box stays empty rather than taking the first key, which for
  the delete on the same form would be the worst version of that mistake.

- **Send trims the client's key box before it sends.** The client has always
  trimmed the key on its way to the service, so a request typed as `  kitchen `
  went out as `kitchen` while the box went on showing the spaces. The box is now
  trimmed too, on every form that takes a key, so the key on screen after Send is
  the key that was used. A key of nothing but spaces now shows as the empty box
  it is, beside the warning that says the key cannot be empty.

### Fixed

- **`GET /sign/information` could stop reading partway through the sign's
  answer.** The service stopped as soon as the line had been quiet for 200ms,
  and the sign can pause for longer than that in the middle of a reply: a
  memory configuration read through a reader using the same rule came back cut
  off partway through its second entry. What had arrived still parsed, so the
  result was wrong rather than missing, and the rest of the answer was left on
  the line to be read as the start of the next one.

  A reply is now read until the EOT that closes it, and one that has started
  but not finished within three seconds is a 503 rather than half an answer.
  The general information reply is short and has not been seen to pause, so
  this is a fix for a trap rather than for a wrong answer anybody reported.

- **A `ttl_seconds` now ends within about a second of when it says.** Expiries
  are applied by a sweep, and the sweep ran every fifteen seconds, so a message
  or alert stayed up for anything up to fifteen seconds past its deadline: a ten
  second ttl was timed on a sign lasting eighteen and twenty two. The default
  `registry_sweep_seconds` is now 1. A sweep that finds nothing due writes
  nothing to the sign, so running it more often costs nothing on the line. A
  config file that sets `registry_sweep_seconds` keeps its own value, so one
  copied from the old example still says 15 and should be changed.

## [0.4.0] - 2026-09-10

**This is the release that met the hardware.** Everything in it comes from
driving a real BetaBrite Classic rather than reading the protocol document, and
the document turned out to be wrong about this sign five times and silent about
it once. A message field and seven markup tokens are removed because the sign
does not honour them, so a request carrying `position` is now a 422 and one
using `<wide_on>`, `<dbl_height_on>` or any of the `<date>` tokens is a 400,
where each used to be accepted and quietly draw something else. **Read the
Removed section before upgrading.**

### Added

- **A display mode the protocol document declines to describe: `AUTO_COLOR`.**
  The sign draws the message with a random transition and a random colour. It is
  not `AUTO` with extra steps: `AUTO` shuffles the transition and leaves the
  colour alone, and nothing else offered randomises colour at all.

  Its row in the Standard Modes table reads "reserved" and carries no name and no
  description, which is why the service never offered it and why nothing in this
  project had looked at it. Put on the sign for thirty seconds beside `AUTO` and
  beside `HOLD`, it turned out to be a real mode. `scripts/mode_parade.py` is the
  test that found it and is kept for the next time the table says no.

  One consequence worth knowing before using it: the mode overrides the colour
  the message asks for. The sample was written with an explicit green in front of
  it and came back in changing colours.

- **`GET /sign/information` asks the sign what it is and how it is doing.** The
  firmware build and revision letter, the month that firmware was released, the
  sign's own clock and whether it draws a 12 or 24 hour one, whether its speaker
  is enabled, and the total and unused size of its memory pool.

  This is the service's first read. Everything before it was written and hoped
  for, because the sign cannot acknowledge a write on this protocol version, and
  the service reconciles by re-pushing on a timer instead. A sign that does not
  answer within a few seconds is a 503, the same as a sign that cannot be
  written to, and so is a sign whose answer will not parse: the reply is the
  whole of what the endpoint has, so one it cannot read leaves it with nothing
  to report.

  Two of the fields earn their place. `speaker_enabled` is the answer to "SOUND
  did nothing": the protocol calls disabled the default, and a muted sign beeps
  silently. `memory_free` is the pool a memory configuration draws on, so a slot
  capacity that will not fit is visible before it fails.

  The protocol document calls this read "most useful as a source of
  troubleshooting information", and the raw answer is returned alongside the
  parsed fields for when the parsing is the thing in doubt.

- **Ten more of the sign's own characters can be written.** `₧`, `ƒ`, `ª`, `º`,
  `θ`, `Θ`, a single column space at U+2009, and the accented capitals `Á`, `Ê`
  and `Í`. Write the character itself in a message; there is no token for these,
  the same as for the accented letters that already worked.

  The sign holds sixty-six characters beyond ASCII and only forty-two of them
  could be reached, so a message containing `Á` was answered with "the sign
  cannot display 'Á'" by a service talking to a sign that could. What had held
  the rest back was that the protocol document draws its character column as
  pictures rather than text, so nothing established which mark a code held.
  Reading those pages as images settled it, and each of the ten was drawn on the
  sign beside the character it is mapped from before the mapping was written.

  Fourteen are still out. Codes B0H to B9H look like a Croatian or Serbian set
  and the diacritics at BBH to BDH cannot be told apart at five dots by seven,
  and a wrong mapping would be accepted and silently drawn where an absent one
  is refused with a message saying so.

- **Seven markup tokens for the ways this sign can actually draw text.**
  `<font_normal>`, `<font_half_height>` and `<font_wide>` choose a character
  set; `<bold_on>`/`<bold_off>` and `<extra_wide_on>`/`<extra_wide_off>` switch
  a character attribute. The `font_` three are a selection rather than a
  switch, so `<font_normal>` is the way back from half height.

  These are what survived putting all twenty of the protocol's character sets
  and attributes on the sign and photographing each: twenty codes collapsed
  into five appearances, because a seven-pixel display cannot express the
  difference between seven slim, seven stroke and seven fancy. They are named
  for what a person sees rather than for the document's labels, which
  contradict themselves here, calling `1AH+6` both "ten high standard" and
  "seven stroke fancy" when on seven rows it is ordinary text.

- **A `SPEAKER` control command mutes the sign.** `ON` and `OFF` write the
  sign's speaker enable register, and `OFF` is a mute: `SOUND` is still
  accepted and makes no noise. The setting lives on the sign and survives a
  restart. It is a separate command from `SOUND` deliberately, because a sound
  command that re-enabled the speaker on its way past would leave the mute
  unable to hold, so nothing but this writes that register. It is also the
  answer when `SOUND` appears to do nothing: the protocol calls disabled the
  default, and a sign in that state beeps silently.

- **A `SOUND` control command sounds the sign's speaker.** `TONE` gives one
  continuous tone of about two seconds, `BEEPS` gives three short beeps, and
  those are the only two sounds offered because they are the only two this
  hardware can make. The protocol also has a programmable tone carrying a
  frequency, a duration and a repeat count, which is deliberately not exposed:
  driving a BetaBrite Classic across the whole documented frequency range, `00`
  against `FE`, produced no audible difference, so the sign has a fixed-pitch
  buzzer and drops the frequency. A parameter the sign silently ignores would
  promise control that does not exist.

- **A `SOFT_RESET` control command restarts the sign without erasing it.** The
  protocol has two resets a byte apart, and only one of them is destructive.
  `E,` puts the sign through its power-up diagnostics and keeps everything:
  checked on a BetaBrite Classic by reading the memory configuration, the pool
  counts, the run sequence and two text files back either side of it, all byte
  for byte identical. So this is the first thing to try on a sign that has
  stopped responding, and `POST /sign/reboot` below is the escalation. It joins
  the closed control command set on `POST /sign/command`, since it disturbs no
  file the service tracks, and it takes no parameter. The call waits out the
  diagnostics before answering, because the sign is deaf through them and a
  write sent into that window would go missing rather than be refused.

- **`POST /sign/reboot` resets a wedged sign and restores the display.** A sign
  mounted out of reach can stop responding to writes when a stray bit corrupts
  what its decoder is showing, and cannot be power cycled by hand. This clears
  the sign, which resets it, waits for it to restart, then re-pushes every
  message and the run sequence from the service's own record, so the sign comes
  back showing what it was rather than blank; any active alert is re-asserted
  too. It is disruptive, blanking the sign for about ten seconds, and it is a
  recovery tool rather than a way to clear messages, which `DELETE /messages`
  still does without a reset. It is refused with a 503 when the sign cannot be
  reached, since a sign that is not answering cannot be rebooted. The client
  lists it and fronts it with a warning-coloured confirmation.

- **A way to run against a real sign from a checkout, with the client beside
  it, and the sign's address where it can be edited.**
  `scripts/run_against_a_sign.py` starts the service and the client and no
  simulator, and both editors carry it as "readerboard against the real sign
  and the client".

  The address is a `--serial-url` argument in those configurations rather than a
  setting in a file, so pointing the service at a different adapter is editing
  the Parameters field in PyCharm's run configuration dialog. That is the one
  deliberate exception to keeping a real sign's details out of a tracked file,
  and it is there because changing the address is the thing done most often. The
  API key is not an argument: it stays in `config.local.toml`, which git ignores
  and which the launcher writes with a generated key the first time it runs,
  because a launch configuration is a tracked file and a command line is a shell
  history.

  It keeps a state file of its own, `.local-sign-state.json`, and never discards
  it. Both halves of that are about the one dangerous operation this service
  has. A service with no record of the memory configuration already applied
  writes a new one, and that erases every message on the sign; and
  `scripts/run_with_simulator.py` deletes its own state file on every launch,
  because the simulator starts empty every time, so a shared file would have
  meant a simulator session quietly erasing the sign on the next real run. The
  first run against a sign this machine has never driven still erases it, which
  is the only way to allocate the files it then writes into, and the README says
  so where somebody about to do it will read it.

  The serial URL is checked before anything is opened. `socket://host/:4001`
  looks close enough to right and is not, and pyserial answers it with a bare
  `TypeError` from inside a connection attempt, naming neither the setting nor
  the value. The port the service will bind is checked too, so that a second
  checkout of this repository already holding it is reported rather than
  discovered after the client has been pointed at it. The README, the example
  config and the script's own `--help` all give the four address forms,
  `rfc2217://` among them for an adapter that negotiates telnet rather than
  passing bytes through.

  The two launchers share their process supervision through the new
  `scripts/_supervise.py`, so that starting, streaming and stopping children is
  written once and what each child is given stays with the launcher that gives
  it.

- **The documentation page opens itself when the service is started from an
  editor.** A new setting, `open_docs`, waits for the port to answer and then
  shows `/docs` in a browser. Every launch configuration that starts the
  service sets it, in both `.vscode/launch.json` and
  `.idea/runConfigurations/`, and nothing else does: it is off by default, so
  an installed service on a machine with nobody in front of it opens nothing.
  `packaging/config.example.toml` lists it with the rest of the settings, off
  there as everywhere else.

  The service does the waiting and the opening rather than the editor, which is
  what makes it land on the address actually bound. That matters for the
  configuration that runs against a real sign, whose port comes from a config
  file no launch file knows anything about. Waiting for the port rather than
  sleeping a fixed time is what makes it right on a slow start, when the link
  to the sign is opened before the socket is. A browser that cannot be
  launched, a machine with no browser, or a port that never answers is a log
  line and nothing else; the service drives a sign whether or not anybody is
  looking at a page.

- **`readerboard --reload` restarts the service when the source changes.** It
  is what the "readerboard against the loopback" configuration in VSCode used
  to get by invoking uvicorn directly. That configuration now runs the
  service's own entry point, as the PyCharm one of the same name always has, so
  both read settings the same way. Started by hand, uvicorn reads none of them,
  which is why the setting above could not have reached it. watchfiles is not a
  dependency, for the reason `pyproject.toml` gives, so uvicorn polls the tree
  instead and a save takes a moment to be noticed.

- **A `settle_delays_enabled` setting, for saying the far end is not a sign.**
  The service waits out the windows in which the sign cannot listen, after a
  reset or a tone. This turns that off, and should be left on for a real sign;
  the sign simulator has no power-up diagnostics to run and no speaker to switch
  its port off for, so its launcher sets it false and saves twelve seconds on
  every start.

  It is a setting of its own rather than a reading of `inter_packet_delay`,
  which is what governed the wait when it was first written. The two are not the
  same question. Pacing is how fast this end may talk; a settle is how long the
  far end is deaf, and no amount of pacing changes that. Since
  `inter_packet_delay` is documented, adjustable and accepts zero, anyone who
  measured their sign as needing no pacing and set it there would have lost the
  three second wait after a tone without being told, along with the writes that
  landed in it.

### Changed

- **The sign's clock is now set one minute ahead of the real time.** Always,
  and there is no setting for it.

  The protocol's Set Time carries four digits, `HHMM`, and no seconds. A sign
  told "14:33" at 14:33:45 does not start that minute forty-five seconds in, it
  starts it from scratch, and rolls to 14:34 a full minute later at 14:34:45. So
  the sign reads behind for the whole minute, by up to fifty-nine seconds, and
  the error is one-sided: it is never early, only ever late. This sign's own
  drift runs slow on top of that, so the two compound.

  Leading by a minute moves the same one-sided error to the other side. Worst
  case the sign reads a minute fast, best case exact, never slow.

  The day of the week is derived from the shifted moment too, which matters for
  one minute a day: at 23:59:30 the sign is told 00:00 and has to be told
  tomorrow's day to go with it. The lead is added to the instant before the
  timezone conversion rather than after, so that 01:59:30 on the morning the
  clocks go forward produces 03:00 rather than an 02:00 that does not exist.

  `synced_at` on `POST /sign/sync-clock` still reports when the sync happened,
  not what the sign was told. Those were the same value before and are not now;
  reporting a time in the future would read as a bug.

- **A message write is refused with a 503 when the sign is unreachable, rather
  than accepted and held.** `PUT /messages/{key}` used to keep a write it could
  not deliver and converge when the link returned, so a caller learned the sign
  was unreachable only by reading `/health`. It now lets the failure through as
  the 503 that alerts, the clock and control commands already gave, with the
  reason in the `detail` body, so a client can tell at once that its message did
  not reach the sign. The registry is left exactly as it was: a new message is
  the caller's to retry when the link is back, and a failed update keeps the
  message that was already there. Messages already on the sign are still pushed
  again when the link returns, which is a separate path.

  The write also fails fast now. A write while the link was down went through a
  fresh connection attempt, which against a wrong or dead network address is the
  operating system's whole connect timeout, so a request could hang for the best
  part of a minute before it was answered. The link is opened only by the
  reconnect loop and at startup; a write to a link that is down is refused at
  once. This bounds the wait when the sign cannot be reached; it does nothing for
  a sign power cycled behind a still-connected adapter, where the socket stays up
  and the periodic refresh is what repairs the display.

- **`tests/test_launch_configurations.py` now covers both editors.** It checked
  that PyCharm could parse its files; it also checks that `.vscode/launch.json`
  loads, that every configuration in either editor which starts the service
  asks for the documentation page, and that the ones which start no service do
  not. A configuration that quietly lost the setting would still run perfectly
  and simply stop opening a tab, which is not the sort of thing anybody reports.

### Removed

- **The vertical text position is gone, and it is a breaking change.** The
  `position` field on a message and on an alert, the `position` in both
  responses, and `GET /enumerations/text-positions` have all been removed. A
  request that still carries a `position` is answered with 422 rather than
  silently ignored, because a silent success would leave the caller believing
  the sign had honoured it.

  All four values drew the same thing. The protocol document says so in the note
  closing that list, "On one-line signs, the Display Position is irrelevant",
  and a Betabrite is one line, seven pixels of it, with no second line for text
  to sit above or below. Confirmed on the sign. This is the same measurement
  that retired `<wide_on>` and `<dbl_height_on>`.

  The byte itself still goes out on every write, because the protocol requires
  it even where it does nothing, and the sign simulator still names all four
  because it decodes whatever reaches it. Nothing about what appears on the
  display changes.

  A stored slot or alert written by an earlier version still loads. The state
  file's version is deliberately unchanged, since a version mismatch would make
  the service start empty, and an empty state has no record of the applied
  memory configuration, which erases every message on the sign.

- **The `<wide_on>`, `<wide_off>`, `<dbl_height_on>` and `<dbl_height_off>`
  markup tokens**, replaced by ones that do something. All four were put on the
  real sign and drew text pixel-identical to no markup at all. A Betabrite is
  seven pixels high: nothing can be twice as tall as the whole display, and the
  protocol's own "enable wide characters" turns out to draw plain text on it
  too. See **Added** above for the seven tokens that took their place, and
  `docs/protocol-notes.md` for the measurement. A message still containing one
  is not rejected: restored content is re-rendered leniently, so the tag comes
  back as literal text, and the sign simulator still annotates the codes.

- **The `<date>`, `<date_dmy>` and `<date_long>` markup tokens.** They inserted
  the sign's own date, and on this hardware that date cannot be made correct.
  The sign stores a two-digit year, and the windowing that would read `26` as
  2026 is gated by the protocol's Table 15 footnote 15 to "Alpha protocol
  version 2.0 and greater", which Table 3 says a Betabrite is not: it is listed
  as EZ KEY II and Alpha 1.0 only. So the sign applies no century, nothing the
  service could send would fix it, and every one of these tokens rendered a
  confidently wrong date. `<date_long>` was the worst of them, drawing a
  four-digit year out of a field that has no century in it.

  A message that still contains one of these is not rejected. Restored content
  is re-rendered leniently for exactly this case, so the tag comes back as
  literal text rather than failing to load, and the sign simulator still
  annotates the underlying control codes. `<time>` and `<week_day>` are
  unaffected and stay: both are registers of their own that the clock sync
  writes at startup, hourly and on every reconnect, so both are right.

### Fixed

- **A write sent just after `SOUND` could be swallowed by the sign.** The
  protocol switches the sign's serial port off for the length of a tone and asks
  for three seconds before anything else is sent: "the tone generation command
  must be the last transmission frame because the sign's serial port is disabled
  (and cannot receive any data) while a tone is generated."

  Only `SOFT_RESET` waited. A beep is not a restart, so `SOUND` asked for no
  wait and the next write went out one `inter_packet_delay` later, half a second
  by default, into a sign that was not listening. Nothing failed loudly: the
  transport accepted it, the controller's suppression cache recorded the file as
  holding those bytes, and the message stayed missing until the next periodic
  re-push up to fifteen minutes later.

  `SOUND` now holds the sign's lock for three seconds, so other writers queue
  rather than write into the gap, and the request returns when the sign is
  listening again. That wait is governed by `settle_delays_enabled` above and
  by nothing else, so pacing the line differently cannot take it away.

- **A sign left off over a weekend could leave the service answering 503 until
  it was restarted.** The reconnect backoff computed its delay as
  `backoff_initial * 2 ** (failures - 1)` from a counter that never stopped
  rising. At the sixty second cap that reaches attempt 1025 in about seventeen
  hours, where `1.0 * 2 ** 1024` raises `OverflowError` rather than returning
  infinity, because the base is a float and the result cannot be represented.
  An `ArithmeticError` was not what anything on the way out was catching, so it
  escaped and killed the task watching the link.

  That task became load bearing in this release: a write used to open the link
  lazily and now refuses when it is down, which is what makes a write to an
  unreachable sign a 503 instead of a long hang. So the watcher is the only
  thing left that opens anything, and with it dead the service stayed down
  after the sign came back, with `/health` stuck at degraded and a 503 body
  claiming the next attempt was due in `0.0s`. The delay is now carried forward
  and doubled rather than recomputed from the count, and neither the watcher
  nor startup can be killed by anything a single open attempt raises.

- **A write sent while the sign was restarting was silently lost.** The sign is
  deaf from the moment a reset reaches it until its power-up diagnostics finish,
  and a write arriving in that window is not refused: the adapter takes it, the
  suppression cache records it as delivered, and the caller is answered 200. The
  message was simply never on the sign, and suppression then kept it from being
  re-sent until the next periodic refresh. Both reset paths now hold the sign's
  lock across the whole restart, so another caller's write queues behind it
  rather than being thrown at a sign that cannot say it missed it. This covers
  the memory clear inside `apply_memory_config`, which had the same window
  between the clear and the configuration, and which the hourly clock sync could
  reach.

- **A stored alert could stop the service starting, permanently.** An alert is
  re-rendered leniently when it is restored, so that a markup token a newer
  version no longer knows cannot keep it from coming back. That leniency makes
  it longer, because the unknown tag returns as its own literal text, and an
  alert near the sign's fixed 125 byte priority file could outgrow it. The
  resulting error is a `ValueError`, not a `TransportError`, so it walked past
  the startup handler that tolerates an unreachable sign and out of the
  application lifespan: the service then failed to start on every attempt, and
  the only cure was editing the state file by hand on the machine. Such an alert
  is now released with a warning naming it, and the startup path no longer lets
  anything in a state file stop the service coming up.

- **Four places said an empty write releases the sign's priority file.** This
  branch measured that it does not: an ordinary write with no text still carries
  a Start-of-Message byte, a position and a mode, which the sign reads as a blank
  priority message and displays. The claim survived in `AlertRequest`'s OpenAPI
  description, which shipped to every consumer, in `write_priority`'s docstring
  three lines above the `clear_priority` docstring that says the truth, in the
  alert service's own module docstring, and in two tests. The reason an empty
  alert is refused is corrected with it: not that the sign would hand itself
  back, but that it would sit blank with the rotation suppressed behind it.

- **Releasing an alert no longer hides every message on the sign.** The release
  wrote the priority file with an empty body but with the Start-of-Message byte,
  a position and a mode still attached, the shape of an ordinary text write. A
  BetaBrite Classic reads that as a blank priority message and holds the whole
  screen on it, rather than as the "write without any ASCII message" the
  protocol releases on. Because the service clears the priority file on every
  start, to let go of an alert a previous run might have left up, a blank
  priority takeover was suppressing every slot from the first moment the service
  ran: alerts displayed, and the rotation never did. The release is now the bare
  write the sign wants, `A0` and nothing after the label. Found against the sign,
  where the bare form brought the rotation straight back and the old form blanked
  it. `docs/protocol-notes.md` records it and `tests/test_frames.py` pins the
  bare form.

- **A memory configuration is now written to a sign that will display it.** The
  BetaBrite Classic stored a configuration, echoed it back on a read verbatim,
  and then showed nothing from any file it named, unless a bare `E$` clear was
  sent first. `SignController.apply_memory_config` now clears before it
  configures, which reaches the same erased sign a step sooner and so adds no
  risk to what was already the one destructive operation, and waits a moment
  after the clear for the reset it triggers. Reconfiguration is rare, only when
  the slot pool changes, so the wait is paid almost never.
  `tests/test_controller.py` pins the clear-then-configure order.

- **The log carries uvicorn's lines again, so there is an HTTP access log.**
  Every request the service answered, the startup banner and the address
  actually bound had been going nowhere. `logging_setup.configure` silenced
  propagation on the uvicorn loggers, which is the right thing to do when
  uvicorn configures its own handlers and the wrong thing here: the service
  starts it with `log_config=None`, so those records had no handler at either
  end and were dropped. Nothing else was affected, which is why it went
  unnoticed. The service started, served, and drove the sign in silence about
  every request it answered.

  `tests/test_logging_setup.py` pins both halves of it, because the obvious
  repair for one is the cause of the other: an access line reaches the
  handlers, and it reaches them once.

- **An empty alert message is now rejected rather than acted on.** `POST
  /alerts` with `""` wrote an empty priority file, which is the protocol's own
  release sequence, and then recorded the alert as active. The sign handed
  itself back and `GET /alerts` went on reporting an alert nothing was
  displaying. The field now carries `min_length=1`, so pydantic answers 422 and
  the OpenAPI description says why. Only an empty string could do this: every
  other message renders to at least one byte.

  A service upgraded with one already in its state file lets it go on the next
  start, rather than restoring it and repeating the release write forever.

  `PUT /messages/{key}` takes the same floor, for a different reason. An empty
  message there is not the release sequence, it is a slot held open around
  nothing: the sign cycles to a file with no text in it and the pool is a slot
  smaller for it. `DELETE /messages/{key}` is how a slot is given back, and it
  always was. A service upgraded with one already in its state file drops it on
  the next start and hands the file back to the pool.

- **A control command parameter of digits the sign never meant is now a 400.**
  `SET_TIME` and `SET_DAY_OF_WEEK` guarded their parameter with `str.isdigit`
  and then called `int` on it, and the two do not accept the same characters. A
  parameter of superscript twos passed the guard and failed the conversion,
  which reached the caller as a 500 in plain text rather than as one of the
  service's own errors. The Arabic-Indic digits passed both, so `٠٩٣٠` quietly
  set the sign to 09:30. The guard now asks for ASCII decimal digits, which is
  what "four digits, HHMM on a 24 hour clock" already promised.

- **`scripts/run_with_simulator.py` now honours a `READERBOARD_API_KEY` the
  machine already sets.** It builds the environment it starts the service in,
  and it set that variable from its own option every time, so a key set
  anywhere else was discarded without a word. The service came up, answered,
  and refused every write carrying the key its owner had every reason to think
  was in use. The option still wins where it is given and the development key
  is still the fallback, so nothing that worked before behaves differently; an
  empty variable counts as unset.

  What made it worth fixing is where the option has to be written in an editor.
  Both configurations that run this script are tracked files shared with
  everyone who opens the project, and PyCharm rewrites one of them in place the
  moment a field in it is edited, taking the comment that explains it with it. A
  variable set on the machine needs none of that. The configurations that start
  the service directly are unchanged and could not work this way: an editor
  environment block wins over the same name inherited from the machine, so the
  key those use is still the value in the file, and each one now says so.

  `tests/test_run_with_simulator.py` pins the order, and pins that the help
  text does not print a key it found in the environment. That output is the one
  that gets pasted into a message to somebody else.

## [0.3.0] - 2026-09-02

**Every path changes, and three settings are removed.** The second, older HTTP
surface is gone, and with nothing left for a version prefix to distinguish, the
remaining paths lose theirs: `/v2/messages` is now `/messages`. Every caller
rewrites the address it posts to, and a config file naming one of the removed
settings stops the service booting until the line is deleted. **Read the Removed
section below before upgrading a machine that is running.**

No byte this service sends to a sign changes. Otherwise, what changes is where
the protocol constants come from, how the state file is written on Windows, and
a second tool alongside the simulator for exercising the API by hand.

### Removed

- **The `/Write` and `/Enumerations` surface is gone.** `POST /Write/Message`,
  `POST /Write/ControlCommand` and the three `/Enumerations` reads answered a
  fixed body at a fixed path, with the outcome carried in the body as `result`
  and `result_message`. Everything they did, `/messages`, `/sign/command` and
  `/enumerations` do, and that surface also serves text positions, which the
  older one never had.

  A caller migrates by rewriting its path and its body. The message write is the
  one that changes shape rather than only address:

  ```
  POST /Write/Message  {"display_mode": "HOLD", "message": "..."}
  PUT  /messages/YOUR-SLOT-NAME  {"display_mode": "HOLD", "message": "..."}
  ```

  Choose the slot name; it is the thing the older surface never let a caller
  say, and it is why several sources can share the sign. The control command
  write becomes `POST /sign/command` with the same `command` and `parameter`
  fields, and answers 204 rather than a body. Failures are reported in a
  `detail` field under a status code that already meant the same thing on the
  surface that remains.

- **The `/v2` prefix is gone from every remaining path.** It distinguished that
  surface from the older one beside it. With the older one removed it
  distinguishes the surface from nothing, so `/v2/messages/{key}` is now
  `/messages/{key}`, `/v2/alerts` is `/alerts`, and so on through all fifteen
  operations. `docs/openapi.json` lists them; `/docs` on a running service is
  the same list.

- **A slot named `default` is left behind on an upgraded machine.** The message
  write owned one reserved slot under that name, and the state file outlives the
  code that wrote it. Nothing in the service writes to it any more, but the
  refresh loop goes on pushing it to the sign every fifteen minutes, so the last
  message it held stays on the display for good. Remove it once, after
  upgrading:

  ```
  curl -X DELETE http://localhost:5001/messages/default -H 'X-API-Key: YOUR-KEY'
  ```

  A 404 means there was nothing to remove, which is the answer on a machine that
  never used that surface.

- **Three settings are removed, and an unknown setting stops the service
  starting.** Configuration is validated with `extra="forbid"`, so a
  `config.toml` naming any of these raises at startup and the service refuses to
  boot. `scripts/install.sh` keeps an existing config file on upgrade, which is
  exactly the file that may carry one. The environment form is not the same: a
  `READERBOARD_` variable matching no setting is ignored rather than refused, so
  a container passing one starts normally. Delete these lines before restarting:

  - `default_slot_ttl_seconds`, which expired the reserved slot the removed
    message write owned. `ttl_seconds` on a message write does the same thing
    per message and is unaffected.
  - `default_display_mode` and `default_text_position`, which have been read by
    nothing since the first commit. They were declared, validated, documented in
    the example config and printed by `--print-config` as settings in force, so
    the program stated a fact about itself that was not true.
    `packaging/config.example.toml` no longer lists any of the three.

- **Lenient rendering is no longer reachable over HTTP.** The removed message
  write passed an unknown markup token through to the sign as literal text. A
  write now earns a 400 saying which token the sign does not have, so a caller
  is told rather than shown something it did not ask for. Nothing already stored
  is affected: a slot or an alert put back from the state file is still
  re-rendered leniently, so content accepted once cannot fail to come back
  because the rules around it tightened.

### Added

- **The sign simulator and the client each have an icon.** Both showed
  Python's icon on the taskbar and Qt's stock one in the title bar. Each now
  carries a drawing of its own beside its code, and the two are deliberately
  nothing alike, so that they are told apart at a glance: the simulator's is
  the sign itself, amber dots on black, and the client's is a paper plane on a
  blue tile. On Windows each tool also names its process to the taskbar,
  so the taskbar shows the same icon as the title bar and the two tools get a
  button each when run together, rather than sharing one under Python's.

- **A desktop client for the HTTP surface, `tools/apiclient/`.** The simulator
  stands in for the sign; this stands in for a caller. It can call all fifteen
  endpoints, and the fifteen are checked rather than claimed: its endpoint table
  is diffed against `docs/openapi.json` for every endpoint in both directions,
  and for each one the body field names and which of them are required. A route
  or a field added here fails that tool's tests in the same commit.

  It also asks each address it is pointed at for that service's own description,
  once, and reports when the surface it finds is not the one it was built for.
  Pointed at a Pi running an older release, it says so rather than leaving the
  difference to be discovered as a 404 that looks like a bug in the client.

  Responses are read back as text rather than printed as JSON, with a formatter
  for each shape the service answers with, including the null alert, the 204,
  and both error shapes. Two things in it are load bearing rather than
  cosmetic. The enumerations are empty until a button is pressed, so the markup
  tokens a message field offers are the ones this service answered rather than a
  copy that went stale in the client. And an error is not only a 4xx: a body
  that is not JSON at all is one too, whatever the status above it, which is
  what being pointed at a proxy error page on the right port looks like. A
  client that coloured by status code alone would show one of those in green.

  It is a tool, not part of the service. Qt stays out of `pyproject.toml`,
  `tools/` stays in `.dockerignore`, and the client has its own hash-pinned lock
  file, the fourth in the tree. It needs no dependency the simulator did not
  already have, because its HTTP client is `QtNetwork`.
  `tools/apiclient/README.md` has the rest.

- **`scripts/run_with_simulator.py --with-client` starts all three at once.**
  The script already brought up the simulator and the service pointed at it.
  The flag adds the client pointed at the service, so the whole loop comes up
  from one command with no sign in the room. Both editors have it as
  "readerboard, the sign simulator and the client", beside the two way one
  they already had.

  Three things about it are deliberate. Closing the client leaves the other
  two running, which closing either of those does not: the service and the
  simulator are no use without each other, and a closed client is only
  closed. The client takes no API key from a command line by design, so the
  key in use is printed for pasting rather than passed to it. And starting
  the client this way writes the base URL into the settings it remembers
  between runs, replacing an address left in it from last time.

  The PyCharm half of it parses now. Its comment spelled the flag out, and
  two hyphens in a row are not allowed inside an XML comment, so PyCharm
  logged an error and left the configuration out of its run list without
  saying so on screen. `tests/test_launch_configurations.py` now parses every
  file under `.idea/runConfigurations/`, so the next one fails the suite
  rather than a person's afternoon.

### Changed

- **The sign simulator's window is rearranged, and its state is no longer behind
  tabs.** What the sign would be showing is now a band across the top of the
  window, always visible, naming the files being cycled or rendering the
  priority message that is suppressing them. It was the first row of a panel
  behind a tab, which meant the window could be open and busy and never answer
  the question the tool exists for.

  The log spans the full width underneath it, and the byte by byte reading and
  the sign's state sit side by side below that. The five tabs are gone: the four
  that describe the sign are now collapsible sections on one scrolling column,
  each header carrying its own row count, so a write that changes the run
  sequence is visible where it lands instead of waiting for somebody to open the
  right tab. The three that are empty until a command arrives start shut and
  open themselves the first time they hold a row.

  The Notes tab is gone as a pane, because it was a filtered view of the log
  rather than anything the sign holds. The toolbar has **Only rows with notes**
  in its place, which hides every transmission the sign had nothing to say
  about. Nothing is discarded: clearing the filter brings the rows back, a saved
  transcript holds every transmission either way, and the status bar says how
  many rows are being held back, because a filtered log and a log that has
  stopped arriving look the same.

  Nothing about what the simulator decodes, keeps or complains about changes.

- **Which exception means which status code lives in one table**,
  `readerboard/api/errors.py`. Every entry in it registers the handler that
  answers it, so the mapping and the wiring cannot come apart, and an exception
  the table does not name reaches no handler of ours and is a 500. That is the
  honest answer for something the service never planned for: it cannot say whose
  fault it was.

- **`readerboard/protocol/constants.py` is regenerated from the protocol
  document.** It was vendored from a repository that carries no licence file,
  which the README named as the one part of this project whose provenance was
  not cleanly MIT. That paragraph is now gone, because the thing it described
  is.

  Every value is transcribed from the Alpha Sign Communications Protocol, form
  9708-8061 revision F, and each section of the file cites the table or page it
  came from. The names and the values are unchanged: the names are the internal
  interface and most of them are the protocol's own vocabulary, and the values
  are protocol facts rather than anybody's expression. What is new is the prose,
  the ordering, which now follows the document's own, and the citations.

  Nothing about what reaches a sign changes, and that is checked rather than
  asserted: every one of the 337 constants was compared against the previous
  file, and the 319 that remain hold the byte for byte identical value.

- **Eighteen constants are gone**, none of them referenced by the service, the
  simulator, the tests or the scripts. Among them were two duplicate spellings
  of a mode the BetaBrite names differently, a second name for the carriage
  return, and three `PRINT3_` constants whose values were plainly wrong: they
  held the ASCII text `_01` where the pattern of the constants beside them calls
  for a byte. Nothing referenced them, so nothing had ever noticed.

- **The character attribute table is pinned**, which nothing did before. It was
  the largest group of constants that `tests/test_constant_values.py` did not
  cover. Two further tests pin the other half of the Alpha 1.0 constraint, that
  the modes and display positions the document marks Alpha 3.0 are absent, since
  those sit in the same tables as the ones this sign does use.

- **`constants.py` no longer needs its `ruff` exemptions.** The vendored table
  was a wall of assignments with trailing comments, which needed `E501` and two
  ambiguous-character rules turned off for it. The regenerated file passes with
  nothing disabled, so the per-file entry is deleted rather than updated.

- **The sign simulator's window title changed, and its names now come from one
  place.** It answered to three at once: the directory and `prog=` said
  `signsim`, the Qt application name said "readerboard sign simulator", and the
  window title said "BetaBrite sign simulator". The title bar and the taskbar
  entry contradicted each other, and both contradicted the directory. The window
  now says "readerboard sign simulator", the application name is `signsim` so
  that it matches the directory it lives in, and both are read from
  `signsim/names.py` rather than written out separately.

  Nothing the simulator decodes or displays changes. It saves no settings of its
  own, so there is nothing stored under the old application name to lose.

- **The editor launch configurations agree on one name.** The configuration that
  starts the service and the simulator together was "API and sign simulator" in
  `.vscode/launch.json` and "API and simulator" in `.idea/runConfigurations/`,
  so the two editors disagreed about a thing the README describes once. "API"
  was also a fourth word for the service, used nowhere else in the tree. Both
  are now "readerboard and the sign simulator", the other PyCharm configurations
  lead with `readerboard` in the same way, and the files under
  `.idea/runConfigurations/` are renamed to match the configurations they hold.

- **The loopback launch configuration says what it runs against.** It was
  "readerboard against a fake sign" in both editors, sitting beside "readerboard
  and the sign simulator", and the two names were the wrong way round: the
  simulator is the thing that behaves like a fake sign, and `loop://` is not a
  sign at all. It is pyserial's loopback, so nothing on the far end acts on a
  byte, keeps a file table or objects to anything. Both editors now call it
  "readerboard against the loopback", which is the word `README.md` already uses
  for it, and the file under `.idea/runConfigurations/` is renamed to match the
  configuration it holds.

### Fixed

- **A state save could fail on Windows, and take the request down with it.** The
  write is made atomic by renaming a temporary file over the old one, and Windows
  refuses that rename while any handle is open on either file. A virus scanner
  opens a file the moment it is written, so the rename failed at random. Measured
  on one machine with Defender running, about one save in thirty five raised
  `PermissionError`. Nothing catches it, so a write would have answered 500.

  The rename is now attempted up to five times, four of them retries. The second
  attempt is immediate, because in the same measurement a single immediate retry
  cleared every occurrence: the scanner's handle is gone within microseconds.
  Only the attempts after that wait, a hundredth of a second apart, and the wait
  is worth avoiding in the common case because a save runs on the event loop.

  Atomicity was never in question and is not changed: a failed rename left the
  previous state file whole, and still does. Removing the temporary file
  afterwards can fail for the same reason the rename did, so that is logged
  rather than raised, and the failure a caller sees is the one that matters
  rather than one about the tidying up. The file is then remembered and removed
  by the next save, and anything an earlier run abandoned is cleared at startup,
  so a directory that stays locked cannot collect one file per failed save.

  The Raspberry Pi this service is written for runs Linux, where a rename over an
  open file is legal, so the second attempt is never reached and nothing there
  changes.

- **The sign simulator and the client open inside the screen.** Each asked for
  a fixed size and never looked at the desktop it was opening on. A scaled
  desktop has fewer logical pixels than its panel suggests, and on one at 300%
  both came up with their bottom edge behind the taskbar, looking right only
  once maximised. Each now shrinks to fit the screen's working area with a
  small margin all round, counting the title bar, and centres itself. On a
  desktop with room for it, nothing changes.

  The client could not shrink far enough on its own: its enumerations panel
  set a minimum height that a 1080p desktop at 150% does not have above the
  taskbar. The panel now scrolls when it is shorter than its rows need, with
  the bar appearing only then, so the window can be as short as such a desktop
  requires.

### Documentation

- **The package description no longer promises scheduling.** It read "several
  sources share one sign, with alerts, scheduling and clock sync". A TTL is
  expiry rather than scheduling, and the protocol's own per-file start and stop
  times are a feature this service deliberately bypasses with `FFFF`, so the
  word promised something that does not exist. It now reads "expiring
  messages", which is what the TTL actually does.

  The sentence lives in three places and was wrong in all three. Two of them
  are in the tree and change here: `project.description` in `pyproject.toml`,
  which is what PyPI renders, and the `org.opencontainers.image.description`
  label in the `Dockerfile`, which is what a registry shows against the image.
  The third is the repository description on GitHub, which is not in the tree
  and has to be set there.

- **What each runnable thing is called is written down, and pinned.** There are
  three of them: the service, the sign simulator and the client. Each answered
  to three or more names, and the surfaces disagreed. That is how a window title
  came to contradict a taskbar entry, how the two editors came to give one
  launch configuration two names, and how a CI step came to say "relay client"
  for a tool that spelled itself one word everywhere else.

  `AGENTS.md` now carries the table and the rule behind it. Each component has
  an identifier for code, paths and settings keys, a display name for what a
  person reads on a window or in a unit file, and a prose name for sentences.
  The tiers are separate because they are not equally expensive: the client's
  identifier is also where QSettings keeps its remembered base URL, so a
  cosmetic rename of it would silently orphan that value. Each component reads
  its own three names off a `names.py` beside its code, so one component's
  surfaces can no longer drift apart from each other.

  `tests/test_component_names.py` pins the table. It also fails if a retired
  name reappears anywhere in the tree, and that second half is the one that
  earns its place: pinning a constant catches an edit to the constant, and does
  nothing at all about an old name creeping back into a README, a CI step name
  or a launch configuration. This file is exempt from that scan, because a
  released entry is a record of what was true when it shipped and rewriting the
  names in it would make the history a worse guide, not a better one.

  The client was renamed as part of this, from `relayclient` to `apiclient`.
  "Relay" appeared nowhere else in the project, in code or in prose, so the
  name's first half referred to a concept this project does not have. It has not
  appeared in a release, so nothing that shipped changes and there is no stored
  setting to migrate.

## [0.2.0] - 2026-08-30

No path, request body, response body, status code or setting name changes, so
nothing an existing client sends has to change. What is here is a development
tool that lives beside the service rather than in it, a correction to how the
service describes its own authentication to the documentation page, another
documentation audit, and a round of work on the workflows and the release
plumbing. The one change that reaches an installed system is the container's
base image, which moved from Python 3.13 to 3.14.

### Added

- **A sign simulator, `tools/signsim/`.** A PySide6 application that stands in
  for the BetaBrite. It listens on a TCP port and decodes everything written to
  it, showing each transmission byte by byte with the protocol's own meaning
  beside each span, and keeping the state the sign would now be in: the file
  table, the contents of each file, the run sequence and the priority file.

  Pointing the service at it needs no change to the service. `serial_url`
  already takes any pyserial URL, because that is how a sign on an Ethernet to
  RS-232 adapter is reached, so `socket://127.0.0.1:4001` is the whole
  integration.

  The state is the reason it exists. A capturing transport shows one packet at a
  time, and the failures worth catching are in the sequence rather than in any
  single packet: a message written to a file the run sequence does not name, an
  alert never released, a reconfiguration that erases everything at the wrong
  moment. The simulator names each of those in the words of the protocol
  document.

  It is a debugging aid rather than a validator, and the distinction is written
  down where it will be read: it decodes against `readerboard.protocol`'s own
  tables, so it agrees with the encoder by construction and cannot tell you that
  a byte value is the one the document asks for.

  Qt is not a dependency of the service and does not become one. The simulator
  has its own hash-pinned lock file, `tools/` is excluded from the container
  build context, and `pyproject.toml` gains no extra. The image builds for
  `linux/arm/v7` under emulation, which settles it.

- **One command to run the service and the simulator together**,
  `scripts/run_with_simulator.py`. It starts the simulator, reads its listening
  address back from its own output rather than probing the port, which would
  show up in the window as a client that never speaks, and then starts the
  service pointed at it. Ctrl+C stops both.

  It discards the service's state file first, and that is the point of it rather
  than a detail. The service reconfigures the sign's memory only when the plan
  changes, which is right against a sign that keeps its memory across a restart;
  the simulator starts empty every run. Pairing the two with a state file left
  from last time means no memory configuration is sent at all, and every write
  to anything but file `A` is then refused for a pool that was never allocated.
  `--keep-state` opts out, for testing that path deliberately.

- **Editor run configurations for both halves and for both at once.** PyCharm
  gains `.idea/runConfigurations/`, and `.vscode/launch.json` gains the combined
  one beside the two it already had. `.idea/` is no longer ignored wholesale,
  because those configurations are as much a shared part of the checkout as the
  `launch.json` beside them. The per-user half of that directory, the window
  layout and the local interpreter path, stays ignored.

### Changed

- **The API key is declared as a security scheme rather than as a header
  parameter.** It was an ordinary `X-API-Key` header parameter on each protected
  operation, which worked but told neither the documentation page nor a
  generated client that it was a credential. The Swagger UI at `/docs`
  consequently had no **Authorize** button, and the key had to be pasted into
  every endpoint separately. Now it goes in once.

  Nothing changes for a client. The same header carries the same value, a
  missing or wrong key is still a 401 with the same wording, and a service with
  no key configured is still a 503. The scheme is declared with
  `auto_error=False` for exactly that reason: left at its default it would
  answer for itself and collapse those two cases into one generic message.

  `docs/openapi.json` is 135 lines shorter, because one scheme replaces the
  per-operation parameter on nine operations, and the eleven open operations now
  say plainly that they need nothing. Anyone generating a client from it gets a
  better one.

- **The Swagger UI remembers the key across a page reload**, through
  `persistAuthorization`. Without it every reload meant another trip to the
  config file.

- **CI type checks the simulator.** The lint job installs its lock file and runs
  the `mypy` invocation its README documents, which nothing ran before. Same
  reasoning as the container smoke test: a thing exercised only by hand rots
  quietly.
- **`pytest` at the root now collects the simulator's tests as well.** They
  import only its pure half, never PySide6, so they run in CI where Qt is not
  installed. Among them is a round trip that pushes the output of every frame
  builder in `readerboard/protocol/frames.py` back through the decoder and
  checks it reads as the command that built it.
- **`scripts/lock_hashes.py` covers three lock files rather than two**, and
  names them by path when reporting, since two of them are called
  `requirements.lock`.
- **The image publishing lives in `mjaksn/workflows` now**, called from here and
  pinned by commit like any other third-party step. It was the same hundred and
  forty lines in three repositories, kept in step by hand, and they had begun to
  drift. The two registries stay separate calls, so a Docker Hub outage still
  cannot hold up the release page, and CI gains a `rehearsal` job that runs the
  GHCR half with `push: false` on one platform, so the shared file is exercised
  on every pull request rather than first at tag time.
- **The container's base image is `python:3.14.6-slim`, pinned by digest rather
  than by patch tag.** A patch tag is quieter than a rolling one but it is not
  still: while it is the newest of its line it is rebuilt too, so its digest
  keeps moving. The digest pins the exact bytes, and the one chosen is old
  enough to satisfy the standing rule against anything published within seven
  days. This is also the move from Python 3.13 to 3.14 inside the image.
- **The release workflow refuses a tag that is not on `main`.** A tag put on a
  release branch before the squash merge names a commit that never reaches
  `main`, and `git describe` on `main` then answers with the release before it.
  Refused now, in the same job and voice as the check that the tag and the
  version agree.
- **Dependabot watches the actions, the Python packages and the base image**,
  with a seven day cooldown so nothing released within the last week is ever
  offered.

### Removed

- **The CodeQL workflow.** It was the only workflow pinning its actions by tag
  rather than by commit, nothing required its check to pass, and nothing else
  referenced it. Stated plainly since this reduces coverage rather than adding
  it: the repository no longer runs static analysis on pushes, pull requests or
  a schedule. Dependabot, `ruff` and `mypy` are unaffected and keep running in
  CI.

### Documentation

- Prose audited across every surface it lives in, again. The claim that the
  simple routes answer 200 to everything gained its exceptions everywhere it is
  stated, sixteen other stale claims were corrected, and the changelog link to a
  0.1.0 release that was never tagged is gone.

## [0.1.4] - 2026-08-28

Release plumbing and documentation only. Nothing in the HTTP surface changes,
and an existing install has no reason to move for it. The reason it is a
release at all is that most of what changed only happens when a tag does.

### Changed

- **The two registries are separate jobs now.** GHCR authenticates with the
  workflow's own token and cannot fail for want of a secret; Docker Hub needs a
  stored one, so a rotated token or an outage there now costs the Docker Hub
  mirror and nothing else. The image is built twice rather than retagged across
  the two, which is the price of sharing nothing between them, and not a risk of
  divergence: the base image is a digest and every dependency is a version and a
  hash, so the second build has the same inputs as the first.
- **The GitHub release waits for the image.** The notes lifted out of this file
  say the image is published on every release, so a release page cut while that
  job was failing would announce a pull that answers "manifest unknown".
- **The base image is pinned to a patch tag, `python:3.13.14-slim`, rather than
  the rolling `3.13-slim`.** A rolling tag is rebuilt every few days, so whatever
  digest it points at is always a few days old. A patch tag stops moving once the
  next one ships, so it can be both specific and old enough to use. The contents
  are the same Debian and the same tzdata.
- **`latest` is `latest=auto` rather than `latest=true`.** A prerelease tag such
  as `v1.0.0-rc1` would otherwise move `latest` onto it, and `latest` is what an
  unqualified `docker pull` takes.

### Added

- **The Docker Hub page now carries `README.md` as its overview**, pushed on
  every release. That page is a field on the repository rather than part of the
  image, so no label reaches it and it stayed blank however well the image was
  labelled. One consequence worth knowing: it only moves when a tag does, so a
  README change lands there at the next release rather than at the merge. This
  needs the Docker Hub token to have write access, not only push.

### Fixed

- The licence link in `README.md` pointed at `LICENSE.md`, which does not exist.
  It now points at `LICENSE`, by absolute URL, so that it also works on the
  Docker Hub page, which renders the README somewhere the repository's relative
  paths mean nothing.

## [0.1.3] - 2026-08-27

### Added

- **A container image**, published to `ghcr.io/mjaksn/readerboard` and
  `docker.io/mjaksn/readerboard` on every release, for `linux/amd64`,
  `linux/arm64` and `linux/arm/v7`. It is a peer of `scripts/install.sh` rather
  than a replacement for it: the same service, the same settings, and the same
  state file, on a machine that runs containers instead of systemd.
  `packaging/docker-compose.yml` is a worked example, including what a sign on a
  cable rather than on the network needs.
- `scripts/lock_hashes.py`, which fills in the lock files from the index's own
  digests. It reads the PyPI JSON API and downloads nothing.
- `requirements-build.lock`, pinning and hashing setuptools, the one package
  needed to build a wheel from this tree. Both the image and
  `scripts/install.sh` now build with `--no-build-isolation` against it instead
  of letting pip fetch an unverified setuptools of its own. The
  `requires = ["setuptools>=77"]` floor in `pyproject.toml` is unchanged, since
  that is what anyone else building from source resolves against.

### Changed

- **Plain `uvicorn` instead of the `uvicorn[standard]` extra**, which drops
  `uvloop`, `httptools`, `PyYAML`, `websockets` and `watchfiles` from the
  dependencies. That extra makes a busy server faster, and this one is not busy:
  it answers a handful of requests and then waits on a 9600 baud serial line
  behind a deliberate inter-packet delay, so a faster event loop and HTTP parser
  buy nothing measurable here. The cost was measurable. None of the first three
  publishes a 32-bit arm wheel, and building them under emulation took eight
  minutes of a thirteen minute container build. Nothing in the HTTP surface
  changes. An existing install keeps the packages until they are cleaned up, and
  is not harmed by them.
- **`requirements.lock` now carries a hash for every file of every pinned
  version**, and both the image and `scripts/install.sh` install with
  `--require-hashes`. A version pin says what to install; the hashes say what the
  bytes must be, and pip now refuses anything else. Nothing about which versions
  are installed has changed.

Nothing in the service itself changed. Every setting the container needs was
already reachable as a `READERBOARD_` environment variable.

## [0.1.2] - 2026-08-26

### Documentation

- The README now carries the same badge set as the sibling projects: CI,
  Release, PyPI version, and licence. Released so that the badges appear on the
  PyPI project page, which is rendered from the README inside the uploaded
  distribution and cannot be edited in place.

No code changed in this release.

## [0.1.1] - 2026-08-26

### Changed

- **The slot `POST /Write/Message` writes to is now named `default`**, and the
  setting controlling its lifetime is `default_slot_ttl_seconds`. Both were
  previously named for what they were rather than what they do, which read as a
  reference to something no longer here.
- **The OpenAPI schema names for that surface are now `Simple*`**, matching the
  documentation, which calls it the simple API throughout. Nothing about the
  request or response bodies changed, only the names the schema gives them.

### Documentation

- Prose audited across every surface it lives in: the README, this changelog,
  `AGENTS.md`, `docs/protocol-notes.md`, docstrings and comments, the prose
  inside the packaging and workflow files, the installer's printed steps, and
  the service's own log messages.
- Passages that explained a design choice by contrasting it with older code now
  state the reason directly. The comparison told a reader nothing, since the
  code being compared against was never part of this project, while sounding as
  though it did.
- Added the licence badge to the README.

## 0.1.0 - 2026-08-26

First release.

### Added

- **Several sources can share one sign.** Each registers a named slot and the
  sign rotates through them by itself, using the Alpha protocol's run sequence.
  Rotation therefore costs no serial traffic at all: the host writes once and
  the sign cycles unaided.
- **Alerts** take the whole display through the sign's priority file and hand it
  back when released, optionally on a deadline. The rotation resumes by itself.
- **The sign's clock is kept correct** at startup, hourly, and whenever the link
  comes back. That last trigger is the one no schedule can manage, because a sign
  returning from a power cut does so at no particular minute.
- **Writes the sign already satisfies are suppressed.** A source re-sending an
  unchanged temperature does not make the display redraw.
- **The registry is persisted**, so a restart puts the sign back as it was
  without every source having to write again, and a write that arrives while the
  sign is unreachable is accepted and delivered when the link returns.
- **A simpler surface** at `/Write/Message`, `/Write/ControlCommand` and
  `/Enumerations`, where every response is HTTP 200 with the outcome in the
  body, for clients that would rather not branch on status codes.
- **An API key** is required on every write, compared in constant time and never
  logged. `GET /health` needs none.
- **A systemd unit and an installer**, both idempotent, with the service running
  as a dedicated user under a hardened unit.
- **`scripts/protocol_spike.py`**, which answers the questions the protocol
  document cannot: whether rotation is seamless on a given sign, whether a run
  sequence write disturbs a running alert, and whether the sign answers reads
  through an Ethernet to RS-232 adapter.

### Fixed

Carried over from the implementation this replaces, where each of these was a
live defect:

- A message containing `<` with no closing `>` looped forever and wedged the
  request thread. The tokenizer now advances on every branch.
- Every message was written to the sign's priority file, which by protocol
  suppresses everything else, so the sign could only ever show one thing.
- Message text was encoded as UTF-8, which the sign has never understood, so an
  accented character rendered as two bytes of noise. Text is now encoded against
  the sign's own character table.
- A dead serial link reported success. It is now a 503, except on the
  simple routes, which answer 200 to everything on purpose.
- The port was opened, written, slept on for two seconds and closed for every
  request, so concurrent callers contended for the device. One writer now owns
  the link and holds it open.

[0.5.1]: https://github.com/mjaksn/readerboard/releases/tag/v0.5.1
[0.5.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.5.0
[0.4.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.4.0
[0.3.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.3.0
[0.2.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.2.0
[0.1.4]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.4
[0.1.3]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.3
[0.1.2]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.2
[0.1.1]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.1
