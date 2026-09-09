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
- **It survives restarts and outages.** The registered messages are persisted and
  pushed to the sign again whenever the link returns, so a restart or a power cut leaves
  the rotation intact. A write that arrives while the sign is unreachable is refused with
  a 503 rather than silently held, so the caller learns it did not land.
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

## Running it against a real sign

From a checkout, with the sign on a cable or on an Ethernet to RS-232 adapter:

```
pip install -e ".[dev]"
pip install --require-hashes -r tools/apiclient/requirements.lock
python scripts/run_against_a_sign.py --serial-url socket://192.168.2.51:4001
```

That starts the service and the client together, with no simulator. The service
comes up on <http://127.0.0.1:5001> with `/docs` beside it, the client comes up
pointed at that address, and the API key to paste into the client is printed in
the same window. `--no-client` leaves the client out. Ctrl+C stops everything,
and closing the client leaves the service running.

Both editors carry it as a launch configuration named "readerboard against the
real sign and the client". **The sign's address is an argument in those, not a
setting in a file**, so changing which sign is driven means editing the
Parameters field in PyCharm's run configuration dialog, or `args` in
`.vscode/launch.json`. They also pass `--api-port 5002`, so a second checkout of
this repository on the same machine can run beside them; the launcher checks
that port before it starts anything rather than letting the service bind, fail
and stop after the client has been pointed at whatever else answered.

### Writing the address

It is a pyserial URL, and **there is no slash between the host and the port**.
`socket://192.168.2.51/:4001` looks close enough to right and is not: pyserial
answers it with a bare `TypeError` from deep inside a connection attempt, naming
neither the setting nor the value. The launcher checks the address before it
opens anything and says which part is wrong. The four forms are:

```
socket://192.168.2.51:4001   an Ethernet to RS-232 adapter passing raw TCP
rfc2217://192.168.2.51:23    an adapter speaking the telnet serial protocol
COM3                         a cable on Windows
/dev/ttyUSB0                 a cable on Linux
```

Most adapters pass raw TCP, so try `socket://` first. If the link opens but the
sign shows nothing or shows rubbish, and the adapter answers on port 23, it is
probably negotiating telnet rather than passing bytes through, and `rfc2217://`
is the form that speaks that.

### The API key, and config.local.toml

The key is not an argument. A launch configuration is a tracked file and a
command line is a shell history, and anyone holding the key can write to the
sign. It lives in `config.local.toml` at the root of the checkout, which
`.gitignore` covers and which the launcher writes with a generated key the first
time it runs. Given no `--serial-url`, the address is read from there too.

### The first run erases the sign

Writing a memory configuration erases every message on the sign, and the service
writes one whenever it has no record of the configuration already applied. The
first run against a sign this machine has never driven therefore erases it,
which is also the only way to allocate the files it then writes into. Every run
after that reads the record and leaves the sign alone.

That record is `.local-sign-state.json`, and it belongs to this launcher alone.
`scripts/run_with_simulator.py` deletes its own `.local-state.json` on every
launch, because the simulator starts empty every time and the service has to
reconfigure it. If the two shared one file, a simulator session would throw the
sign's record away and the next run against the sign would erase it.

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

Make a noise, which is worth pairing with an alert if the sign is somewhere nobody
is watching it:

```
curl -X POST http://localhost:5001/sign/command \
     -H 'X-API-Key: YOUR-KEY' -H 'Content-Type: application/json' \
     -d '{"command": "SOUND", "parameter": "BEEPS"}'
```

`BEEPS` is three short beeps and `TONE` is one continuous tone of about two seconds.
Those are the only two sounds there are: the sign has a fixed-pitch buzzer, so there
is no pitch or volume to choose.

Silence it with `{"command": "SPEAKER", "parameter": "OFF"}`, and turn it back on with
`ON`. That is a real mute: `SOUND` is still accepted and makes no noise. The setting
lives on the sign and survives a restart, so it is also the first thing to check if
`SOUND` ever seems to do nothing.

The full API is at `/docs`. Every markup token, display mode, text position and
control command is listed by the `/enumerations` reads there, which answer at
request time rather than being frozen into the description.

### Writing messages

A message is plain text plus tokens written as `<name>`: `<green>18.4<degree>` is a
colour change, a number, and a degree symbol. `GET /enumerations/markup-tokens` lists
them all.

Text is encoded against the sign's own character table rather than as UTF-8, so `café`
displays correctly. A character the sign cannot render is rejected with a 400, as is an
unknown token: a write is told what the sign would have made of it rather than being
shown something it did not ask for.

### Recovering a sign that has stopped responding

A sign mounted out of reach can wedge: a stray bit corrupts what its decoder is
showing, it stops responding to writes, and there is no power switch within reach.
There are two recoveries, and they are not interchangeable. Try the gentle one first.

**A soft reset restarts the sign and erases nothing.** The sign runs the same power-up
diagnostics it runs when you plug it in, then carries on showing what it was showing.
Its memory, its file table and its messages all survive; this was verified on the sign
by reading them back either side of a reset.

```
curl -X POST http://localhost:5001/sign/command \
     -H 'X-API-Key: YOUR-KEY' -H 'Content-Type: application/json' \
     -d '{"command": "SOFT_RESET"}'
```

The call waits out the diagnostics before answering, so a 204 means the sign is
listening again rather than that the bytes went out.

**`POST /sign/reboot` is the escalation, and it is destructive.** It clears the sign
outright, waits for it to restart, then re-pushes every message and the run sequence
from the service's own record, so the display still comes back to what it was.

```
curl -X POST http://localhost:5001/sign/reboot -H 'X-API-Key: YOUR-KEY'
```

Reach for it only when a soft reset was not enough. The sign is blank for about ten
seconds while it resets. Neither is a way to clear messages: `DELETE /messages` does
that without resetting anything. The client fronts the reboot with a warning-coloured
confirmation for the same reason.

## Configuration

Settings come from `/etc/readerboard/config.toml`, overridden by environment variables
prefixed `READERBOARD_`. `packaging/config.example.toml` documents every one of them.

Every setting has both forms, and the container path relies on it: `slot_count` in the
file is `READERBOARD_SLOT_COUNT` in the environment. Under Docker the file is optional
and usually absent, which is not an error. `READERBOARD_CONFIG_FILE` moves the file if
you want it somewhere other than the default.

The sign's address is a full pyserial URL in `serial_url`: `socket://192.168.2.51:4001`
for an Ethernet to RS-232 adapter, `rfc2217://192.168.2.51:23` for one speaking the
telnet serial protocol, `/dev/ttyUSB0` or `COM3` for a cable plugged straight in, or
`loop://` to run the service with no sign attached. There is no slash between the host
and the port, and pyserial's answer to one that has a slash names neither the setting
nor the value.

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

`scripts/run_with_simulator.py` starts the service and the simulator together, and
with `--with-client` the client as well, so the whole loop comes up from one command.
Both editors carry it as a launch configuration under the same name, "readerboard and
the sign simulator", in `.vscode/launch.json` and in `.idea/runConfigurations/`, beside
configurations for running the pieces separately. Both carry the three way one as
"readerboard, the sign simulator and the client" as well.

`scripts/run_against_a_sign.py` is the other one, for when the sign is real: the
service and the client, no simulator, and the sign's address passed as an argument so
that it can be edited in a run configuration dialog. Both editors carry it as
"readerboard against the real sign and the client". The section above has the rest,
including the one thing about it that is dangerous. The two launchers share their
process supervision through `scripts/_supervise.py` and differ in what each child is
given, which is the part that matters: the simulator launcher discards its state file
on every run and this one never discards anything.

Every one of those that starts the service sets `READERBOARD_OPEN_DOCS`, so `/docs`
opens in a browser once the port answers. The service does the waiting and the
opening, which is why it lands on the port actually bound rather than one repeated in
a launch file. The setting is off unless asked for, so an installed service opens
nothing.

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
