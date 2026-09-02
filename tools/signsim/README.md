# readerboard sign simulator

A stand-in for a BetaBrite Classic. It listens on a TCP port, decodes everything
the service writes to it, and shows both what the bytes mean and what the sign
would be holding as a result.

It exists because the interesting failures are not in the frame builders, which
`tests/test_frames.py` already pins byte for byte. They are in the sequence: a
message written to a file the run sequence does not name, an alert that never
gets released, a reconfiguration that erases everything at the wrong moment. A
capturing fake transport shows you one packet at a time. This shows you the sign.

No hardware is needed, and neither is the service: it decodes whatever is sent
to it.

## Running it

```
pip install --require-hashes -r tools/signsim/requirements.lock
python scripts/run_with_simulator.py
```

That starts the simulator and the service together, with the service already
pointed here, and stops both on Ctrl+C. It is the usual way in. Both editors
have it under the same name, "readerboard and the sign simulator", in
`.vscode/launch.json` and in `.idea/runConfigurations/`.

To run only the simulator, because the service is already up somewhere else:

```
python tools/signsim/run.py
```

It listens on `127.0.0.1:4001` and prints the setting to paste into the service:

```
READERBOARD_SERIAL_URL=socket://127.0.0.1:4001
```

That is the whole integration. `serial_url` already takes any pyserial URL,
because that is how a sign on an Ethernet to RS-232 adapter is reached, and
nothing in the URL says the far end has to be an adapter. Nothing in the service
changes, and no setting is added to it.

One thing the launcher does that is worth knowing if you start the two by hand:
it deletes the service's state file first. The service records the memory
configuration it applied and reconfigures only when the plan changes, which is
right against a sign that keeps its memory across a restart. This does not; it
starts empty every time. Pair a fresh simulator with a state file left from the
last run and the service sends no memory configuration at all, and every write
to anything but file `A` is then refused for a pool that was never allocated.

`--host` and `--port` move it. A name is resolved, so `--host localhost` works
as well as `--host 127.0.0.1`, and an IPv4 result is preferred when a name gives
both, because the address is printed straight back as the `serial_url` to paste
into the service. The default binds the loopback address only, which is
deliberate: this accepts and interprets whatever is sent to it, and there is no
reason for that to be reachable from the rest of the network unless somebody
asks for it.

## What is on screen

**The log**, top left, is one row per transmission, with the command and a
one line reading of it. A write comes back as the markup that produced it, so
`1C 31 48 49` reads as `<red>HI` rather than as hex. Rows are coloured when
something is wrong with them.

**The detail pane**, under it, is the selected transmission byte by byte. Every
span is coloured by what it is, with the protocol's own meaning beside it:

| Colour | What it marks |
| --- | --- |
| framing | the wakeup nulls, `SOH`, the sign type, the address, `STX`, `EOT` |
| command | the command code and the file or function label it names |
| control | a sequence that changes how later text is drawn, or inserts a value |
| text | printable ASCII, which the sign draws as itself |
| glyph | a byte that draws one character ASCII has no room for |
| unknown | a byte in none of the protocol's tables |

**The tabs**, on the right, are the sign itself: what it would be showing now,
the contents of each file, the memory configuration, the run sequence, and every
note the model has raised.

The notes are the part worth having. A real sign accepts a write to an
unconfigured file, or a message longer than the file it goes in, and shows you
the consequence rather than the cause: a blank panel, or a sentence with its end
missing. The simulator says which rule was broken, in the protocol's words. It
catches, among others:

- a write to a file no memory configuration has allocated, which the sign
  discards, since only the priority file and the default file `A` may be written
  before one arrives;
- a memory configuration erasing every file in the memory pool, naming what was
  lost. Not the priority file: that one always exists, sits outside the pool, and
  is not among the four things the document says cancel a priority message, so
  an alert survives a reconfiguration;
- a message longer than its file, and how much of it survives;
- a run sequence naming a file that does not exist, or one allocated as a STRING
  or DOTS picture rather than a TEXT file, both of which the sign skips;
- a Write TEXT aimed at a label allocated as something other than a TEXT file;
- a priority message over the fixed 125 bytes;
- a transmission too truncated to act on, which is left to change nothing rather
  than applied with the decoder's placeholder values. A write cut off inside its
  start of mode would otherwise read as the empty priority write that releases
  an alert, and a truncated clock write would set the clock to 00:00;
- a run sequence written while an alert is up, which is open question 3 in
  `docs/protocol-notes.md`.

## What it does not do

There is no simulated display and no animation. Rendering the message is a
larger job than reading it, and animating the thirty-odd display modes is larger
again; a mode is shown as its name, which is what a person debugging actually
needs. The one thing that would be worth adding is a display panel, and the
thing standing in the way is that the Classic's pixel geometry is not recorded
anywhere in this repository.

It is also one way. Read commands are decoded and shown, and nothing is sent
back. Answering them would make this a rehearsal target for the reads in
`readerboard/protocol/frames.py`, which have never been tried against the
adapter; that is the obvious next thing.

## How it is put together

The modules split on one line, and the line is load bearing:

| Module | Qt | What it does |
| --- | --- | --- |
| `framing.py` | no | pulls whole `SOH` to `EOT` frames out of a byte stream |
| `spans.py` | no | splits a body into runs of text, glyphs and control codes |
| `decode.py` | no | reads a payload as the command it spells |
| `model.py` | no | applies a command the way the sign would, and says what it did |
| `names.py` | no | the three names this tool answers to, and the name it gives the taskbar |
| `server.py` | yes | the TCP listener |
| `window.py` | yes | the window |
| `app.py` | yes | the command line |

The five pure modules import nothing from PySide6, which is why their tests run
in CI where Qt is not installed. They are collected by the root `pytest`, so
`pytest` at the top of the repository runs them along with everything else.

Type checking is a separate invocation, because the root `mypy` config names
only the service:

```
MYPYPATH=tools/signsim mypy tools/signsim/signsim
```

CI runs that too, in the lint job, after installing the lock file above. It is
there for the reason the container smoke test is: an invocation only ever run by
hand is one that rots without anybody hearing about it. `ruff check .` at the
root covers this tree already.

The split also decides where a test can live. Anything needing a `QTcpSocket` or
a widget cannot go in `tools/signsim/tests`, because that suite runs where Qt is
absent. `FrameScanner.reset` is tested there; that pausing the capture calls it
is not.

Beside the modules are `icon.svg` and `icon.ico`. The first is the drawing, a
sign showing a message in amber dots, and the one to edit; the second is what
the window loads, holding the drawing at each size Windows draws an icon at.
`scripts/render_icons.py` makes the second from the first, and its `--check`
runs in CI so that an edited drawing cannot be committed without its rendering.
The client's icon is deliberately nothing like it, a paper plane on a blue
tile, so that the two are told apart at a glance on a taskbar.

On Windows the icon would reach the title bar and not the taskbar without one
more step, because the taskbar files a window under its process's executable,
which for a Python program is `python.exe`. `app.py` names the process to the
taskbar before it has a window, as `readerboard.signsim`, which is what puts
the same icon in both places and gives the simulator and the client a taskbar
button each when they are run together. One thing to know when the drawing
changes: the taskbar keeps the last icon it showed for that name for a while
after the window closes, so a relaunch within a minute or so can show the old
icon on the taskbar and the new one in the title bar. Waiting, or signing out
and back in, clears it.

## The limit worth knowing

The decoder reads `readerboard.protocol`'s own tables. That keeps it in step
with the service for free: a token added there is understood here with nothing
copied. It also means the two agree by construction. This tool can tell you
which token was sent; it cannot tell you that the token's byte value is the one
the protocol document asks for. `tests/test_constant_values.py` in the service is
what checks that, and it is the only thing that does.

So: a debugging aid, not a validator. Do not reach for a green window here as
evidence that a protocol constant is right.

## Dependencies

`PySide6-Essentials` rather than the `PySide6` meta package, because the meta
package also pulls in Addons, which is a large download for modules this never
imports. Both it and `shiboken6` are pinned by version and hash in
`requirements.lock`, refreshed by `scripts/lock_hashes.py` along with the
service's two and the client's.

None of this reaches the service. The wheel, `scripts/install.sh` and the
container image all ignore this directory, and `tools/` is in `.dockerignore` so
it never enters the build context. That is not tidiness: the image builds for
`linux/arm/v7` under emulation, and Qt has no business anywhere near that.
