# relayclient

A desktop client for exercising the readerboard service's HTTP surface. It calls
every endpoint the service has, shows what came back as text rather than as
JSON, and knows no vocabulary it was not told.

Think of it as a friendlier Swagger page rather than as a Postman. It has almost
no logic of its own: what it adds is the ability to load the service's own
enumerations and then reuse them in the fields that take them.

```
python tools/relayclient/run.py
python tools/relayclient/run.py --base-url http://192.168.2.40:8000
```

Qt is not a dependency of the service and must not become one. This tool has its
own hash-pinned lock file, `tools/` is in `.dockerignore`, and nothing in
`pyproject.toml` mentions either of them.

```
pip install --require-hashes -r tools/relayclient/requirements.lock
```

## What it does

**Every endpoint, on one screen.** All twenty operations are listed at once,
grouped by what they act on. Selecting one swaps the form beside it. There is no
drill-down and no wizard: the things that would need a quarter of the window to
show properly, a set of markup tokens or a call's full detail, open as dialogs
instead.

**Nothing is hardcoded.** The markup tokens, display modes, text positions and
control commands all start empty. Press the button beside a set and the client
calls the endpoint for it; the row then says how many arrived, from which
endpoint and when, and a View button opens the list. Only then do the fields that
use that set offer it: before that they are free text, and the button that
inserts a markup token into a message is disabled and says which button to press
first.

That is the point of the design rather than an inconvenience. A client that ships
its own copy of the vocabulary is a client that goes on offering a token for a
year after the service stopped answering it.

Two endpoint families answer the same four sets in two different shapes. `/v2`
names each entry `name`; the simple endpoints use `token_text`, `display_mode` or
`control_command` depending on which was asked. Both are offered, because the
client has to be able to call either, and both normalise to the same store.
Text positions are `/v2` only; the simple surface has no endpoint for them.

**Responses are read, not dumped.** Every shape the service answers with has a
formatter: the health breakdown, the slot table, the alert, the clock, both
enumeration shapes, the simple surface's result, and a 204 rendered as the
success it is rather than as an empty panel. Anything unrecognised falls back to
a labelled walk, so a field added to the service shows up as a row instead of
breaking the view. No raw JSON reaches the main panel.

**An error is not the same as a 4xx.** The simple surface answers HTTP 200 to
everything and reports the outcome in the body, on purpose, for clients that do
not branch on status codes. A tool that coloured by status alone would show a
failed write in green. This one treats a 4xx or 5xx *or* a simple-surface
`result` of `ERROR` as a failure: the status strip turns red and the full
response opens in a dialog.

**It says when it is pointed at a different service than it was built for.**
The catalogue below describes the surface in this checkout. A Pi that has not been
updated still answers the surface it shipped with, and without a word about it the
way you would find out is a 404 that looks like a bug in the client. So the first
time an address answers anything, the client asks it for its own
`/openapi.json` and reports the difference: the version it found, what this client
offers that the service does not have, and what the service has that this client
cannot call. It is one line beside the address, and a dialog when the two disagree.

That check is not a call you made, so it runs on its own connection, stays out of
the history, and never occupies the one in-flight slot. A service that will not
hand over its description is reported as unchecked rather than as broken, and
calls are unaffected either way.

Two details keep it from being noise or from going quiet. It runs only after a
call has actually been answered, so starting the client before the service is up
produces one report of the failure rather than two. And an address that could not
be reached is put back in the pile, so the next call that succeeds asks again,
while one that answered and simply had no description to give is left alone.

Every request also carries a transfer timeout. Qt disables them by default, and
with one call in flight at a time a connection that is accepted and then never
answered would otherwise leave Send disabled for the rest of the run.

**History, in the shape a network tab has it.** Every call this run has made,
with the request line, headers, body, status, timing and the response both read
back and exactly as it arrived. Any of them can be copied as a curl command.

The history lives for the run and goes when the window closes. That is a limit
rather than an unfinished feature: nothing needs a file format and nothing
anybody sent ends up on disk.

## The API key

The key is sent as `X-API-Key` on the writes that need it, and it is treated as a
secret everywhere else:

- It is not saved between runs. The base URL is; the key is not.
- There is no command line option for it, because a key on a command line is a
  key in the shell history.
- It is redacted when a history record is made, not when one is displayed, so
  there is no display path left to forget about.
- The curl command therefore refers to `$READERBOARD_API_KEY` rather than
  containing the key, which is what makes it safe to paste into a script, a
  ticket or a message to somebody else.

## How it is put together

Everything except `net.py`, `dialogs.py`, `window.py` and `app.py` imports no Qt.
That is not tidiness: it is what lets the half holding the logic be tested in CI,
where Qt is not installed. The sign simulator splits the same way for the same
reason.

| module | Qt | what it is |
| --- | --- | --- |
| `catalogue.py` | no | every operation as data: method, path, fields, which enumeration each field draws on |
| `skew.py` | no | comparing a live service's description against that catalogue |
| `request.py` | no | inputs to a request, and the curl equivalent |
| `format.py` | no | responses to readable blocks, and those to HTML |
| `enums.py` | no | the loaded sets, normalised from either shape |
| `history.py` | no | the run's calls, with the key already gone |
| `net.py` | yes | `QNetworkAccessManager`, one call in flight at a time |
| `window.py` | yes | the one screen |
| `dialogs.py` | yes | enumerations, errors, history |
| `app.py` | yes | the command line and `main()` |

The forms are generated from `catalogue.py` rather than written out one by one,
which is what makes "it can call any endpoint" a property of a table rather than
a claim about a window. Hand-written tables drift, so
`tests/test_catalogue.py` diffs it against `docs/openapi.json`: every endpoint in
both directions, and for each one the body field names, which of them are
required, and which operations carry a body at all. A route or a field added to
the service fails this tool's tests in the same commit.

What the description cannot supply is the reason the table is written by hand
rather than generated. It declares no enumerations at all: `display_mode` is a
string with a default, because the service validates against `tokens.py` at
request time rather than freezing the vocabulary into the schema. So which field
draws on which set, which response gets which formatter, and which text fields
take markup are all decisions no generator could read out of it.

One thing in the table is not from the schema and is named so it cannot be
mistaken for it. `prefill` is what a field starts out holding. Where the schema
declares a default the two must agree, and a test insists on it; where it declares
none the prefill is this client's own convenience, which is true in exactly one
place and a test pins that too.

The HTTP client is Qt's own `QtNetwork`, which is why this tool needs no
dependency the simulator does not already have. There is no requests, no httpx
and no certifi to pin.

One call is in flight at a time. That is not worth engineering around: the thing
on the other end drives a single sign down a 9600 baud line behind one lock, so
overlapping calls would tell you less than they appear to.

## Deliberately not here

No saved collections, no environments, no scripting, no response assertions. Each
of those would make this a worse Swagger page and a poor Postman.

## Testing

```
pytest tools/relayclient/tests
```

Runs without Qt, and is collected by the repository's own `pytest` run. Because
of that, nothing in it builds a window, so CI additionally constructs one
offscreen in the job that already has Qt installed. A signal connected to an
attribute that does not exist yet raises on construction and nowhere else, and
that is otherwise nobody's job to notice.
