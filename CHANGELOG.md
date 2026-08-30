# Changelog

Notable changes to readerboard. Versions follow [semantic
versioning](https://semver.org/spec/v2.0.0.html): while the major version is 0
the interface may still change, and any such change is called out here under
**Changed** rather than assumed to be obvious from the version number.

What is versioned is the HTTP surface: the paths, the request and response
bodies, the status codes, and the settings names. The `readerboard` package is
importable and its modules are documented, but it is a service rather than a
library, and the names inside it may move without that being a breaking change.

## [Unreleased]

Nothing in the HTTP surface changes and nothing an install runs is touched. What
is added is a development tool that lives beside the service rather than in it.

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

## [0.1.0] - 2026-08-26

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

[0.1.4]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.4
[0.1.3]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.3
[0.1.2]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.2
[0.1.1]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.1
