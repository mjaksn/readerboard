# Changelog

Notable changes to readerboard. Versions follow [semantic
versioning](https://semver.org/spec/v2.0.0.html): while the major version is 0
the interface may still change, and any such change is called out here under
**Changed** rather than assumed to be obvious from the version number.

What is versioned is the HTTP surface: the paths, the request and response
bodies, the status codes, and the settings names. The `readerboard` package is
importable and its modules are documented, but it is a service rather than a
library, and the names inside it may move without that being a breaking change.

## [0.1.0] - 2026-08-26

First release. This is a ground-up rewrite of an earlier Flask service that
drove the same sign, and nothing of that implementation survives except the
protocol constants table.

### Added

- **Several sources can share one sign.** Each registers a named slot and the
  sign rotates through them by itself, using the Alpha protocol's run sequence.
  Rotation therefore costs no serial traffic at all: the host writes once and
  the sign cycles unaided.
- **Alerts** take the whole display through the sign's priority file and hand it
  back when released, optionally on a deadline. The rotation resumes by itself.
- **The sign's clock is kept correct** at startup, hourly, and whenever the link
  comes back. That last trigger is the one a crontab line could not manage,
  because a sign returning from a power cut does so at no particular minute.
- **Writes the sign already satisfies are suppressed.** Re-sending an unchanged
  temperature no longer makes the display redraw.
- **The registry is persisted**, so a restart puts the sign back as it was
  without every source having to write again, and a write that arrives while the
  sign is unreachable is accepted and delivered when the link returns.
- **A compatibility surface** at `/Write/Message`, `/Write/ControlCommand` and
  `/Enumerations`, reproducing the previous service's request and response
  bodies exactly, including its habit of returning HTTP 200 with an error in the
  body.
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
  compatibility routes, which keep their original behaviour on purpose.
- The port was opened, written, slept on for two seconds and closed for every
  request, so concurrent callers contended for the device. One writer now owns
  the link and holds it open.

[0.1.0]: https://github.com/mjaksn/readerboard/releases/tag/v0.1.0
