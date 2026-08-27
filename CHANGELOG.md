# Changelog

Notable changes to readerboard. Versions follow [semantic
versioning](https://semver.org/spec/v2.0.0.html): while the major version is 0
the interface may still change, and any such change is called out here under
**Changed** rather than assumed to be obvious from the version number.

What is versioned is the HTTP surface: the paths, the request and response
bodies, the status codes, and the settings names. The `readerboard` package is
importable and its modules are documented, but it is a service rather than a
library, and the names inside it may move without that being a breaking change.

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

[0.1.3]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.3
[0.1.2]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.2
[0.1.1]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.1
[0.1.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.0
