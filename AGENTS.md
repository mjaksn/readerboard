# Working on readerboard

An HTTP service that drives a BetaBrite Classic sign over the Alpha protocol,
either through a serial cable or through an Ethernet to RS-232 adapter.

## Work in a worktree, not in this checkout

**Cut a worktree and work in that, before making the first change.** The
failure this guards against is two agents editing one checkout at the same
time, and every part of it is quiet: each reads the other's half-finished
edits as its own, a test fails for a reason nothing in the branch explains,
and a commit carries away work belonging to a change somebody else was in the
middle of. Nothing announces any of it, and the repository root is where a
person is most likely to be sitting.

So fetch, and cut a branch from `origin/main` into a worktree under
`.claude/worktrees/`, which is where this repository already keeps them and
which `.gitignore` covers. `EnterWorktree` does it, an `Agent` given
`isolation: "worktree"` does it, and by hand it is
`git worktree add .claude/worktrees/<name> -b <name> origin/main`.

Two things a worktree does not inherit have to be made again inside it, and
both are quiet when they are not.

- **A virtual environment of its own, with `pip install -e ".[dev]"` in it.**
  Borrowing the one beside the main checkout looks like it works and does not.
  An editable install resolves the package to the tree it was made from, and
  `tests/` holds a `conftest.py` and no `__init__.py`, so pytest puts that
  directory on `sys.path` rather than the root above it and nothing points at
  the worktree. A run started in the worktree therefore collects the
  worktree's tests and imports the service from the main checkout, and passes
  or fails against code nobody is editing. This was measured rather than
  feared.
- **The state file, before the service is started at all.** There are two,
  `.local-state.json` for simulator runs and `.local-sign-state.json` for runs
  against a real sign. Each is one machine's file, both are ignored, and
  neither is in a fresh worktree. No state file means no record of which memory
  configuration was applied, and `Layout.needs_reconfiguration` answers True to
  that. Against the sign simulator it costs nothing. Against a real sign it is
  the one dangerous operation described below, and it erases every message on
  it.

  `config.local.toml` no longer saves a worktree the way it used to, and this
  is the change worth knowing about. The sign's address is an argument in the
  run configurations now rather than a setting in that file, so a worktree that
  copies the run configurations has an address and needs no file: it writes
  itself one, with a key generated into it, and starts. Point a worktree at the
  simulator, or take the address out of the Parameters field in the copy.

Three kinds of work stay in the main checkout, and all three are work a
worktree cannot see or cannot reach.

- **Anything about uncommitted work there.** Committing what is already in the
  tree, saying what has changed, or chasing a failure that only happens with
  those edits in place. A worktree has none of it.
- **Anything about the repository rather than the code**: pruning branches,
  tagging a release, listing or removing worktrees. These act on one shared
  git directory whichever checkout runs them, and a branch that is checked out
  in a worktree cannot be deleted at all.
- **Reading, when nothing is going to be written.** Answering a question or
  reviewing what is there costs a worktree nothing and gains nothing, and the
  checkout is likely to be the tree the question is about.

And being told to, which needs no reason.

## Read this first

`docs/protocol-notes.md` records what the Alpha Sign Communications Protocol
actually says about the memory configuration, the run sequence and the priority
file, with the quotation behind each claim. Read it before changing anything
under `readerboard/protocol/`. It also lists the ten questions the document
cannot answer, all of which three sessions with the sign have now settled. Two
of those answers were wrong the first time and were caught on a repeat run, and
both were about something brief on the display; the record of how is kept beside
each answer, because it is the part that generalises.

## The one dangerous operation

**Writing a memory configuration erases every message on the sign.** The
protocol is explicit: "whenever a Memory Configuration is written, the previous
table is overwritten."

So the service allocates its whole file pool once, records the applied plan in
its state file, and reconfigures only when the plan itself changes. Changing
`slot_count`, `slot_capacity`, `variable_count` or `variable_capacity` is
therefore destructive on the next start. It is done deliberately, it is logged
at WARNING, and it must never become something an ordinary message or variable
update can trigger.

Note that the protocol has a second reset which is nothing to do with this one.
`E,`, the `SOFT_RESET` control command, restarts the sign and keeps its memory,
verified on hardware by reading everything back either side of it. It is the
gentle recovery and carries none of the warnings below. The two are a byte
apart, so read which one a change means.

There is one place the dangerous one runs on demand: `POST /sign/reboot`, the
recovery path for a sign whose decoder has wedged out of reach and which a soft
reset did not bring back. It clears the sign deliberately to reset it, then
re-pushes every slot and the run sequence from the service's own record, so the
erase is followed at once by a restore and the display comes back rather than
staying blank. `MessageRegistry.reboot` is the whole of it; it is gated behind
the API key like every other write, and the client fronts it with a
warning-coloured confirmation. This is the exception the paragraph above allows
for, not a hole in it: a message write still cannot reach the clear, only this
one route asked for by name can.

## What each thing is called

Three things here can be run, and each answers to a name in three tiers. Use the
tier that fits the surface, and coin nothing new.

| | Identifier | Display name | Prose name |
|---|---|---|---|
| the service | `readerboard` | readerboard | the service |
| the sign simulator | `signsim` | readerboard sign simulator | the sign simulator |
| the client | `apiclient` | readerboard client | the client |

- The **identifier** is for code: directories, Python packages, `prog=`,
  `setApplicationName`, lock file paths, `testpaths`, CI job ids. It is the
  expensive one. Renaming it moves directories, imports, and for the client the
  QSettings location that holds the remembered base URL.
- The **display name** is for what a person reads on a window title, a launch
  configuration or a unit file. It leads with `readerboard` so that a window in
  a taskbar says which project it belongs to before it says which part.
- The **prose name** is for sentences: README, this file, CI step names, log
  lines, `--help`. Use the display name on first mention in a document, then the
  prose name. Shortening it further once the document has established which
  thing is meant is fine and often reads better: "the simulator" after "the sign
  simulator" is a short form, not a fourth name. Coining a different word for it
  is what the rule is against.

Each component reads its own three names off a `names.py` beside its code, which
is what stops one component's surfaces disagreeing with each other. They had:
the simulator's directory, application name and window title were three
different strings at once, and the client's identifier was built on a word that
appeared nowhere else in the tree.

`tests/test_component_names.py` pins the table above, and separately fails if a
retired name reappears anywhere in the tree. That second half is the one that
matters, because a constant catches an edit to the constant and does nothing
about an old name creeping back into a README or a CI step.

One ambiguity is worth naming rather than fixing. `readerboard` is the project,
the repository, the PyPI distribution and the service inside it. Where the
difference matters, write the service.

"The API" is not a fourth name for the service, and the places that say it are
right as they stand. It is the HTTP surface the service exposes, which is what
"the API key" and "exercising the API by hand" are about. The service is the
process; the API is what it answers on.

## How it works, in one pass

A **slot** is a named place on the sign that a source owns. Each slot lives in
its own sign file, and the run sequence names the files of the slots that are
showing, in order. The sign cycles them by itself, so a message appearing or
disappearing costs one small write and nothing after that. This is the whole
design: the host does not rotate anything.

A slot can also be **hidden**, which is `PUT /messages/{key}/active` and
`MessageRegistry.set_active`. The run sequence names the active slots and
nothing else, so hiding one is a single sequence write. That write does disturb
the display, measured on 2026-09-12, but far less than rewriting a TEXT file:
short enough to be imperceptible when anything else on screen is changing, and
easy to miss even on static content. A hidden slot keeps its file, its order,
its text and its name, so showing it again needs no copy of the message.

Two parts of that are easy to get wrong. **A hidden slot's file keeps its text**,
so showing it again is one run sequence write and no redraw: the controller still
holds those bytes and declines to send them a second time. The exception is the
last visible message, whose file is emptied as it goes, because a sign handed a
run sequence naming nothing freezes on the message it was drawing and holds it
there, measured on 2026-09-11; without the blank it would sit there for good.
`MessageRegistry._hide` is the whole rule and is the only thing that should
decide it.

And **`active` is three-valued on `PUT /messages`**, which is not the same as
being absent from it. Omitted, it leaves the slot showing or hidden exactly as it
found it, so a source re-sending the same content every five minutes cannot
switch back on something deliberately hidden. Sent, it moves the slot, which is
what lets one call write a message and put it up: a notification with a minute on
it is `active` true with a `ttl_seconds` and `delete_on_expiry` false, sent again
in full the next time the thing it reports changes. `PUT /messages/{key}/active`
stays for hiding something whose text the caller does not have in hand.

A **variable** is a value in a STRING file of its own, which a slot's message
calls with `<var:name>`. Writing one rewrites only that STRING file, and the
sign takes that without blanking or restarting the message calling it, so a
value that changes every minute costs one short packet and no flicker. Three
rules hold it together, and each has a reason that is easy to lose:

- **A variable a message or the alert calls cannot be deleted.** The STRING
  file's label is written into every caller as raw bytes, so handing that file
  to the next variable would put the wrong value on the sign with nothing to say
  so. `MessageRegistry.remove_variable` refuses with a 409 instead, and slots and
  variables share one lock so that nothing can slip between the check and the
  write. The alert service renders through `MessageRegistry.rendering`, which
  holds that lock until the priority file is written and the alert recorded.
  Take the registry's lock before the alert service's, never the other way
  round.
- **Variables are written before messages** on a restore, a refresh and a
  reboot, so no message is drawn calling a STRING not yet written.
- **The size check is not optional.** The sign does not truncate a value that
  overruns its file, it empties it. `docs/protocol-notes.md` has that and the
  rest of what the sign was measured doing, under "STRING files, measured on
  the sign".

An **alert** is written to the sign's priority file, which by protocol
suppresses every other file until a bare priority write releases it. An
ordinary write with an empty body is not a release: the sign reads its
formatting bytes as a blank message and keeps the screen.

`SignController` is the only thing allowed to talk to the sign. Every write goes
through one `asyncio.Lock`, with the blocking pyserial call dispatched to a
worker thread. It also remembers the exact bytes last written to each file and
declines to write them again.

## Things that look wrong and are not

- **Everything is re-pushed on a timer.** The sign and the adapter are
  separately powered, so the sign can be power cycled with the TCP link still
  up. Nothing fires, the write cache stays warm, and suppression would then skip
  exactly the writes that would repair a blank sign.
- **The paths carry no version prefix.** They read `/v2` until the second,
  older surface beside them was removed. With one surface left, a prefix that
  distinguishes it from nothing is a word every caller writes and no reader
  learns anything from. `CHANGELOG.md` has the migration.
- **A route body never decides a status code.** Every failure it can raise is
  one of the service's own exceptions, and `readerboard/api/errors.py` maps each
  to a code once. That table is what registers the handlers, so an exception it
  does not name reaches no handler of ours and is a 500, which is the honest
  answer for something the service never planned for. The failures decided
  before a route body runs are not in it and do not belong there: the 401 and
  the no-key 503 are raised by `require_api_key` in `deps.py`, and the 422 is
  pydantic rejecting the body.
- **`%` formatting throughout, not f-strings.** It matches the lazy `%` that
  logging takes, so one idiom covers a log line and the exception text beside
  it. `UP031` is disabled for this reason.

## Testing

No sign is needed. The suite runs against a capturing fake transport and against
pyserial's `loop://` URL, so the real serial code path is exercised without
hardware.

```
pip install -e ".[dev]"
pytest
ruff check .
mypy readerboard
```

`tests/test_constant_values.py` pins every protocol byte value against the
document, with a citation per assertion. It exists because
`test_every_token_in_the_table_renders` compares the token table to itself and
would pass if every byte in it were wrong. If you add a protocol constant, pin
it there too.

`tests/test_frames.py` asserts whole transmissions literally. When one of those
fails, the frame builder is wrong, not the test.

`pytest` also collects `tools/signsim/tests` and `tools/apiclient/tests`, the
two tools' own suites. Both import only the pure half of their tool, never
PySide6, so they run in CI where Qt is not installed. That leaves one gap, which
CI covers separately: nothing in either suite ever builds a window, so a signal
wired to an attribute that does not exist yet would raise only on construction
and no test would see it. The lint job already installs Qt to type-check the
tools, so it builds each window once offscreen as well. The one worth knowing
about in the simulator round trips the frame builders above through its
decoder: whatever the service builds has to read back as the command that built
it. The one worth knowing about in the client diffs its endpoint catalogue
against `docs/openapi.json` in both directions, so a route added here fails
that tool's tests in the same commit rather than leaving it quietly unable to
call it.

CI also builds the container image for amd64 and starts it against `loop://`,
for the reason it diffs the checked-in OpenAPI description: a thing exercised
only at tag time rots silently, and the first anybody hears of it is a failed
release.

## Watching what goes to the sign

`tools/signsim/` is the sign simulator, a PySide6 application that stands in for
the sign. It listens on a TCP port, and because `serial_url` already takes any
pyserial URL, pointing the service at `socket://127.0.0.1:4001` is the whole
integration. Nothing in the service knows it exists.

It shows each transmission byte by byte, coloured by what each span is and
annotated with the protocol's own meaning, and it keeps the sign's state: the
file table, the contents of each file and each STRING file, the run sequence
and the priority file. The state is what makes it worth having over a packet
log. It says when a write lands in a file no memory configuration allocated,
when a message overruns its file, and when the run sequence names a file that
does not exist.

Two things to know before relying on it. It decodes against
`readerboard.protocol`'s own tables, so it can confirm which token was sent but
never that the token's byte value is right; `tests/test_constant_values.py` is
still the only thing that checks that. And it is one way: read commands are
decoded and shown, and nothing is answered.

Qt is not a dependency of the service and must not become one. It has its own
`requirements.lock`, `tools/` is in `.dockerignore`, and nothing in
`pyproject.toml` mentions it. The image builds for `linux/arm/v7` under
emulation, which is reason enough. `tools/signsim/README.md` has the rest.

## Exercising the API by hand

`tools/apiclient/` is the client, the other end of the same idea: a PySide6
application that calls the service rather than standing in for the sign. Point
it at a running service and it can call all twenty-two endpoints, formats every
response as text rather than JSON, and knows no vocabulary it was not told.

Two things about it are load bearing rather than stylistic. The enumerations are
empty until a button is pressed, so the markup tokens a message field offers are
the ones this service answered rather than a copy that went stale. And an error
is not only a 4xx: a body that is not JSON is one too, whatever the status above
it, because every formatter it has reads a missing value as a value and would
state the absence as a fact. That is what a proxy error page on the right port
looks like, and a tool that coloured by status code alone would show it in
green.

It also asks each address for its own `/openapi.json` the first time that address
answers anything, and says when the surface it finds is not the one the client was
built for. That is what a Pi running an older release looks like, and without the
check it looks like a bug in the client instead.

It splits the same way the simulator does, with the logic in modules that import
no Qt, and it needs no dependency the simulator does not already have because its
HTTP client is `QtNetwork`. `tools/apiclient/README.md` has the rest.

With the simulator on one side and the client on the other, the whole loop runs
with no sign in the room. `scripts/run_with_simulator.py --with-client` brings
all three up from one command, and both editors have that as "readerboard, the
sign simulator and the client". Closing the client leaves the other two running,
which closing either of those does not: they are no use without each other,
and the client is only a thing to poke the service with.

`scripts/run_against_a_sign.py` is the same idea with the sign real rather than
simulated: the service and the client, no simulator. Both editors have it as
"readerboard against the real sign and the client". Three things about it are
load bearing.

The sign's address is a `--serial-url` argument in those configurations rather
than a setting in a file, which is the one deliberate exception to keeping the
real sign's details out of a tracked file. It is there because changing which
sign is driven is the thing somebody does most often, and the Parameters field
is where a person editing a run configuration is already looking. The API key
stays in the ignored `config.local.toml`, because a tracked file and a shell
history are both bad places for it. `tests/test_run_against_a_sign.py` parses
the address out of both editors' files and checks the launcher accepts it, so a
bad one fails the suite rather than a launch.

It never discards its state file, because a service with no record of the
applied memory configuration writes a new one, which is the dangerous operation
above; and its state file is its own, because the simulator launcher deletes
`.local-state.json` on every run and a shared file would mean a simulator
session erasing the sign on the next real one. Both are pinned by reading the
source, since neither can be rehearsed without hardware.

Keep the comment in that PyCharm configuration short. PyCharm rewrites a
configuration file whenever a field in it is edited and drops the comment when
it does, and this is the one configuration whose whole point is that a field in
it gets edited. The explanation belongs in `README.md`, which survives.

The two launchers share their process supervision through
`scripts/_supervise.py`: starting children, tagging and streaming their output,
waiting for a port, and stopping the rest when one that matters goes away. What
each child is given is the part that differs, and it stays in the launcher that
gives it.

Each tool has an icon of its own, an `icon.svg` beside its code and the
`icon.ico` rendered from it by `scripts/render_icons.py`. They are deliberately
nothing alike, so that they are told apart at a glance on a taskbar: the
simulator's is the sign itself, amber dots on black, and the client's is a
paper plane on a blue tile. Edit the SVG and rerun the script; CI runs its
`--check` so that one cannot land without the other. On Windows each `app.py`
also names its process to the taskbar as `readerboard.<identifier>`, which is
what makes the taskbar show the window's icon rather than Python's.

## Prose is part of the product

Log lines, exception text, `--help` output and the OpenAPI descriptions are read
by people, usually at an unwelcome hour, and they go stale exactly as a README
does. A change that alters behaviour should change the prose describing it in
the same commit.

## Releasing

Tag `vX.Y.Z`. The release workflow checks the tag against `pyproject.toml` and
against `readerboard.__version__`, all three of which must agree, and lifts the
release notes out of `CHANGELOG.md`.

A tag publishes three things: the distributions to PyPI, the container image to
GHCR and Docker Hub, and the GitHub release page. PyPI goes through a trusted
publisher and GHCR through the workflow's own token, so neither has a secret in
the repository. Docker Hub is the exception and needs `DOCKERHUB_USERNAME` and
`DOCKERHUB_TOKEN`, and its token needs write access rather than only push.

The two registries are separate jobs. GHCR authenticates with the workflow's
own token, so it cannot fail for want of a secret; Docker Hub needs a stored
one, and in a job of its own a rotated token or an outage there costs Docker
Hub and nothing else. The GitHub release waits for GHCR, so a release page
cannot announce an image that was never pushed.

The publishing itself lives in `mjaksn/workflows` and is called from here,
pinned by commit like any other third-party step. It was the same hundred and
forty lines in three repositories before that, and they had begun to drift.
Two calls rather than one, and that is load-bearing: a called workflow
succeeds only when every job in it succeeds, so a single call covering both
registries would put Docker Hub back in front of the release page. The
Dockerfile stays here, with the thing it packages.

Because the shared file is now one point of failure for three releases, and a
release is the hardest thing here to rehearse, CI calls the GHCR half with
`push: false` on one platform. That is what the `rehearsal` job is, and it is
why the publishing path is exercised on a pull request rather than first at
tag time.

A tag also pushes `README.md` to the Docker Hub page as its overview. That page
is not part of the image and no label reaches it, so without that step it stays
blank however well the image is labelled. GHCR needs no equivalent: it reads the
description and the source link off the image itself. One consequence worth
knowing: the overview only moves when a tag does, so a README change lands there
at the next release rather than at the merge.

The image is built for `linux/amd64`, `linux/arm64` and `linux/arm/v7`, the arm
legs under QEMU emulation. Keep every dependency on a platform that publishes a
wheel for all three. This is why `pyproject.toml` takes plain `uvicorn` rather
than the `standard` extra: that extra's `uvloop`, `httptools` and `PyYAML` have
no 32-bit arm wheel, and compiling them under emulation took eight minutes of a
thirteen minute build, to speed up an event loop that spends its life waiting on
a 9600 baud serial line.

`requirements.lock` carries a hash for every file of every pinned version, and
both the image and `scripts/install.sh` install with `--require-hashes`.
`requirements-build.lock` does the same for setuptools, the one package needed
to turn this tree into a wheel, so that both installers can build with
`--no-build-isolation` rather than fetching an unverified one. The floor in
`pyproject.toml` stays a floor: it is what a third party building from source
sees, and only the two installers here are pinned.

The two tools under `tools/` have a lock file each, the third and fourth in the
tree. Nothing that installs the service reads either, and nothing should: they
pin Qt. They pin the same two packages today, and are still separate files, so
that one tool can move Qt without obliging the other to move on the same day.

When moving a version, edit the pin and then run `scripts/lock_hashes.py`, which
brings the hashes with it from the index's own digests and covers all four
files. `--check` fails if any of them has drifted.
