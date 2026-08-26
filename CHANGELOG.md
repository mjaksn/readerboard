# Changelog

Notable changes to readerboard. Versions follow [semantic
versioning](https://semver.org/spec/v2.0.0.html): while the major version is 0
the interface may still change, and any such change is called out here under
**Changed** rather than assumed to be obvious from the version number.

What is versioned is the HTTP surface: the paths, the request and response
bodies, the status codes, and the settings names. The `readerboard` package is
importable and its modules are documented, but it is a service rather than a
library, and the names inside it may move without that being a breaking change.

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

[0.1.1]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.1
[0.1.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.0
