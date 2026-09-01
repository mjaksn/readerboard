# readerboard

[![CI](https://github.com/mjaksn/readerboard/actions/workflows/ci.yml/badge.svg)](https://github.com/mjaksn/readerboard/actions/workflows/ci.yml)
[![Release](https://github.com/mjaksn/readerboard/actions/workflows/release.yml/badge.svg)](https://github.com/mjaksn/readerboard/actions/workflows/release.yml)
[![PyPI](https://img.shields.io/pypi/v/readerboard)](https://pypi.org/project/readerboard/)
[![GHCR](https://img.shields.io/badge/ghcr.io-readerboard-blue)](https://github.com/mjaksn/readerboard/pkgs/container/readerboard)
[![Docker Hub](https://img.shields.io/docker/v/mjaksn/readerboard?label=docker%20hub&sort=semver)](https://hub.docker.com/r/mjaksn/readerboard)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/mjaksn/readerboard/blob/main/LICENSE)

An HTTP service that drives a BetaBrite Classic sign, either through a serial cable or
through an Ethernet to RS-232 adapter.

Several sources can share the sign at once. Each registers a named **slot**, and the sign
rotates through the registered slots by itself. An **alert** takes the whole display over
until it is released, after which the rotation resumes.

## What it does

- **Many messages, one sign.** Home Assistant can own `temperature` while a doorbell
  automation owns `doorbell`, without either knowing about the other.
- **The sign does the rotating.** Each message lives in its own sign file and the sign
  cycles them on its own, so rotation costs no serial traffic at all.
- **Alerts.** Take the display over, optionally with a deadline, then hand it back.
- **It keeps the sign's clock right**, at startup, hourly, and whenever the link comes
  back. That last trigger is the one that matters: a sign returning from a power cut
  does so at no particular minute.
- **It does not redraw the sign for nothing.** A write of bytes the sign already holds is
  suppressed, so a source re-sending an unchanged temperature does not make the display
  flicker.
- **It survives restarts and outages.** The registered messages are persisted, and a
  write that arrives while the sign is unreachable is accepted and delivered when the
  link returns.
- **Errors are errors.** A dead serial link is a 503 and a message the sign cannot
  render is a 400, each with the reason in the body. Nothing here reports a failure
  under a 200.

## Requirements

- Python 3.11 or newer.
- A BetaBrite Classic, reachable either at a serial device such as `/dev/ttyUSB0` or over
  the network through an Ethernet to RS-232 adapter at `socket://host:port`.
- Either a machine running systemd, for `scripts/install.sh`, or a container runtime, for
  the published image. The service itself runs anywhere Python does; only the installer
  is Linux specific.

## Try it without a sign

`loop://` is pyserial's loopback, so the service will start and serve its API with
nothing attached. From a checkout:

```
pip install -e ".[dev]"
READERBOARD_SERIAL_URL=loop:// READERBOARD_API_KEY=dev-key \
    READERBOARD_STATE_PATH=./state.json \
    python -m readerboard
```

Or without one:

```
docker run --rm -p 5001:5001 \
    -e READERBOARD_SERIAL_URL=loop:// -e READERBOARD_API_KEY=dev-key \
    ghcr.io/mjaksn/readerboard:latest
```

Then open <http://127.0.0.1:5001/docs>.

`loop://` swallows everything written to it, so the service runs but there is
nothing to see. To watch what it would have sent, run it against the sign
simulator in `tools/signsim/` instead:

```
pip install --require-hashes -r tools/signsim/requirements.lock
python scripts/run_with_simulator.py
```

That starts the simulator and the service together, already pointed at each
other, and stops both on Ctrl+C. The simulator decodes each transmission, says
what every byte of it means, and shows what the sign would be holding as a
result. `tools/signsim/README.md` has the details.

## Installing it properly

Two ways, which do the same job. Pick whichever suits the machine.

### With systemd

```
sudo scripts/install.sh --serial-url socket://192.168.2.51:4001
```

This creates a `readerboard` system user, builds a virtual environment in
`/opt/readerboard`, writes `/etc/readerboard/config.toml` with a freshly generated API key,
and enables the `readerboard` service. It prints the key once, and it is safe to run
again after pulling a new version: your config file and key are left alone.

`sudo scripts/uninstall.sh` removes the service and the program but keeps your config and
your registered messages, so reinstalling puts the sign back as it was. Add `--purge` to
remove those too.

### With Docker

The image is published to both registries on every release, for `linux/amd64`,
`linux/arm64` and `linux/arm/v7`, so a Pi pulls the same tag an x86 server does.

```
docker run -d --name readerboard --restart unless-stopped -p 5001:5001 \
    -e READERBOARD_SERIAL_URL=socket://192.168.2.51:4001 \
    -e READERBOARD_API_KEY=YOUR-KEY \
    -v readerboard-state:/var/lib/readerboard \
    ghcr.io/mjaksn/readerboard:latest
```

`packaging/docker-compose.yml` is the same thing as a Compose file, with the settings
worth knowing about written out beside it.

The volume is what matters here. The registered messages are persisted to
`/var/lib/readerboard`, and without it the sign comes back empty after a restart rather
than putting back what was on it.

Every setting is available as an environment variable, so no config file is needed. Mount
one at `/etc/readerboard/config.toml` if you would rather have it, in the format
`packaging/config.example.toml` documents; the environment still wins over the file.

For a sign on a cable rather than on the network, the container needs the device passed
in and needs to be in the group that owns it. The group has to be given as a number,
because the container has no `/etc/group` entry for the host's `dialout`:

```
stat -c '%G %g' /dev/ttyUSB0        # 20 on Debian and Raspberry Pi OS, 18 on Fedora
docker run ... --device /dev/ttyUSB0 --group-add 20 \
    -e READERBOARD_SERIAL_URL=/dev/ttyUSB0 ...
```

## Using it

Every write needs an `X-API-Key` header. Reads and `GET /health` do not. In the
Swagger UI at `/docs`, the **Authorize** button puts it in once for the whole page.

Register a message:

```
curl -X PUT http://localhost:5001/messages/temperature \
     -H 'X-API-Key: YOUR-KEY' -H 'Content-Type: application/json' \
     -d '{"message": "<green>18.4<degree> <red><time>", "display_mode": "HOLD"}'
```

Register a second one and the sign rotates between them:

```
curl -X PUT http://localhost:5001/messages/doorbell \
     -H 'X-API-Key: YOUR-KEY' -H 'Content-Type: application/json' \
     -d '{"message": "<amber>Someone at the door", "ttl_seconds": 300}'
```

Take the sign over for thirty seconds:

```
curl -X POST http://localhost:5001/alerts \
     -H 'X-API-Key: YOUR-KEY' -H 'Content-Type: application/json' \
     -d '{"message": "<red><flash_on>SMOKE ALARM", "ttl_seconds": 30}'
```

The full API, including every markup token and display mode, is at `/docs`.

### Writing messages

A message is plain text plus tokens written as `<name>`: `<green>18.4<degree>` is a
colour change, a number, and a degree symbol. `GET /enumerations/markup-tokens` lists
them all.

Text is encoded against the sign's own character table rather than as UTF-8, so `café`
displays correctly. A character the sign cannot render is rejected with a 400, as is an
unknown token: a write is told what the sign would have made of it rather than being
shown something it did not ask for.

## Configuration

Settings come from `/etc/readerboard/config.toml`, overridden by environment variables
prefixed `READERBOARD_`. `packaging/config.example.toml` documents every one of them.

Every setting has both forms, and the container path relies on it: `slot_count` in the
file is `READERBOARD_SLOT_COUNT` in the environment. Under Docker the file is optional
and usually absent, which is not an error. `READERBOARD_CONFIG_FILE` moves the file if
you want it somewhere other than the default.

The sign's address is a full pyserial URL in `serial_url`: `socket://192.168.2.51:4001`
for an Ethernet to RS-232 adapter, `/dev/ttyUSB0` for a cable plugged straight in, or
`loop://` to run the service with no sign attached.

Two settings reallocate the sign's memory when changed, and **that erases every message
on it**: `slot_count` and `slot_capacity`. The service will do it, and say so loudly in
the log, but they are not settings to fiddle with.

## Security

An API key is required on every write, compared in constant time, and never logged.
Reads and `GET /health` need none, so a monitor can watch the sign without holding a key
that could write to it.

The key is declared to the API description as a security scheme, so the Swagger UI at
`/docs` has an **Authorize** button: enter the key once and every write on the page
carries it. It is the same `X-API-Key` header a client sends, so nothing about a script
or a Home Assistant `rest_command` changes.

That page is configured to remember the key, so it survives a reload or a browser
restart rather than needing to be pasted in again. Convenient on your own machine, and
worth knowing before you use **Authorize** on a shared or kiosk browser, where the next
person to open `/docs` inherits it. Use the browser's Logout in the Authorize dialog, or
just do not authorize there.

**Message content reaches the sign as protocol bytes**, so it is worth knowing what a
client holding the key can do. The markup renderer emits bytes only for tokens it
recognises and for characters in the sign's own table, so arbitrary control sequences
cannot be injected through a message. What the holder of a key can do is display
anything they like on your wall and set the sign's clock. There is nothing beyond the
sign to reach: the service opens one serial link and touches nothing else.

Sensible precautions remain sensible:

- Do not expose the service to the internet.
- Keep `/etc/readerboard/config.toml` mode 0640. Anyone who can read it can write to the
  sign.
- Give the key only to clients you trust, and prefer a firewall allow-list on top.
- The service runs as a dedicated system user under a hardened systemd unit, which is
  worth keeping rather than running it as root for convenience.

Under Docker the same points apply, with different mechanisms:

- The image runs as an unprivileged user, UID and GID 10001, not as root. A bind-mounted
  state directory has to be owned by that number on the host.
- An API key passed as an environment variable is visible to anyone who can run
  `docker inspect` on the container, and to anything that reads the Compose file's
  environment. Mounting a config file mode 0640 keeps it out of both.
- Bind the published port to the loopback address, `-p 127.0.0.1:5001:5001`, unless
  clients on other machines need to reach it.
- Passing a serial device in with `--device` gives the container that device and nothing
  else. It does not need `--privileged`, and giving it that would hand it every device on
  the host.

## Development

```
pip install -e ".[dev]"
pytest
ruff check .
mypy readerboard
```

No sign is needed. The tests run against a capturing fake transport and against
pyserial's `loop://` URL, so the real serial code path is exercised without hardware.

`docs/protocol-notes.md` records what the Alpha protocol actually says about the memory
configuration, the run sequence and the priority file, with the quotations that back each
claim. Read it before changing anything in `readerboard/protocol/`.

`scripts/protocol_spike.py` settles the few questions the document cannot answer about
this particular sign. It is destructive and refuses to run without `--confirm-erase`.

`tools/signsim/` is the sign simulator, a PySide6 stand-in for the sign, described
above. `tools/apiclient/` is the client, a PySide6 application for calling the API by
hand: every endpoint, responses shown as text rather than JSON, and a vocabulary it
loads from the service rather than one compiled into it. The tests of both are
collected by the `pytest` run here and need no Qt installed; the applications do, and
each is pinned separately so that nothing the service installs ever pulls Qt in.

`scripts/run_with_simulator.py` starts the service and the simulator together. Both
editors have it as a launch configuration under the same name, "readerboard and the
sign simulator", in `.vscode/launch.json` and in `.idea/runConfigurations/`, along with
configurations for each half on its own.

## Licence

MIT. See [LICENSE](https://github.com/mjaksn/readerboard/blob/main/LICENSE).

## Credits

The protocol is documented in the Alpha Sign Communications Protocol, form 9708-8061,
published by Adaptive Micro Systems. Every byte value in
`readerboard/protocol/constants.py` is transcribed from that document, and
`tests/test_constant_values.py` pins each one against it with a citation per assertion.

An earlier version of this project took that table from
[jonathankoren/readerboard](https://github.com/jonathankoren/readerboard), which is
recorded here with thanks even though no code from it remains.
