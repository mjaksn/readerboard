# Working on readerboard

An HTTP service that drives a BetaBrite Classic sign over the Alpha protocol,
either through a serial cable or through an Ethernet to RS-232 adapter.

## Read this first

`docs/protocol-notes.md` records what the Alpha Sign Communications Protocol
actually says about the memory configuration, the run sequence and the priority
file, with the quotation behind each claim. Read it before changing anything
under `readerboard/protocol/`. It also lists the four questions the document
cannot answer, which need the sign to settle.

## The one dangerous operation

**Writing a memory configuration erases every message on the sign.** The
protocol is explicit: "whenever a Memory Configuration is written, the previous
table is overwritten."

So the service allocates its whole file pool once, records the applied plan in
its state file, and reconfigures only when the plan itself changes. Changing
`slot_count` or `slot_capacity` is therefore destructive on the next start. It
is done deliberately, it is logged at WARNING, and it must never become
something an ordinary message update can trigger.

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
its own sign file, and the run sequence names the occupied files in order. The
sign cycles them by itself, so a message appearing or disappearing costs one
small write and nothing after that. This is the whole design: the host does not
rotate anything.

An **alert** is written to the sign's priority file, which by protocol
suppresses every other file until an empty priority write releases it.

`SignController` is the only thing allowed to talk to the sign. Every write goes
through one `asyncio.Lock`, with the blocking pyserial call dispatched to a
worker thread. It also remembers the exact bytes last written to each file and
declines to write them again.

## Things that look wrong and are not

- **Run sequence writes are held back while an alert is up.** The document says
  a write to the run time or run day table cancels a running priority message,
  and says nothing either way about the run sequence. Until the spike settles
  it, the safe reading is that it might. See
  `MessageRegistry._apply_run_sequence`.
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
file table, the contents of each file, the run sequence and the priority file.
The state is what makes it worth having over a packet log. It says when a write
lands in a file no memory configuration allocated, when a message overruns its
file, when the run sequence names a file that does not exist, and when a run
sequence write arrives during an alert, which `docs/protocol-notes.md` lists
as one of the four questions only the sign can settle.

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
it at a running service and it can call all fifteen endpoints, formats every
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
