# Working on readerboard

An HTTP service that drives a BetaBrite Classic sign over the Alpha protocol,
either through a serial cable or through an Ethernet to RS-232 adapter.

## Read this first

`docs/protocol-notes.md` records what the Alpha Sign Communications Protocol
actually says about the memory configuration, the run sequence, the priority
file and the character set, with the quotation behind each claim. Read it before
changing anything under `readerboard/protocol/`. It also lists the four
questions the document cannot answer, which need the sign to settle.

## The one dangerous operation

**Writing a memory configuration erases every message on the sign.** The
protocol is explicit: "whenever a Memory Configuration is written, the previous
table is overwritten."

So the service allocates its whole file pool once, records the applied plan in
its state file, and reconfigures only when the plan itself changes. Changing
`slot_count` or `slot_capacity` is therefore destructive on the next start. It
is done deliberately, it is logged at WARNING, and it must never become
something an ordinary message update can trigger.

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
  it, the safe reading is that it might. See `MessageRegistry._apply_run_sequence`.
- **Everything is re-pushed on a timer.** The sign and the adapter are
  separately powered, so the sign can be power cycled with the TCP link still
  up. Nothing fires, the write cache stays warm, and suppression would then skip
  exactly the writes that would repair a blank sign.
- **The simple routes return HTTP 200 with an error in the body.** That is the
  whole point of them: they exist for clients that do not branch on status
  codes. The only thing that makes them return anything else is a missing API
  key, which is a 401.
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

CI also builds the container image for amd64 and starts it against `loop://`,
for the reason it diffs the checked-in OpenAPI description: a thing exercised
only at tag time rots silently, and the first anybody hears of it is a failed
release.

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

When moving a version, edit the pin and then run `scripts/lock_hashes.py`, which
brings the hashes with it from the index's own digests and covers both files.
`--check` fails if either has drifted.
