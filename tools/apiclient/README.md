# readerboard client

A desktop client for exercising the readerboard service's HTTP surface. It calls
every endpoint the service has, shows what came back as text rather than as
JSON, and knows no vocabulary it was not told.

Think of it as a friendlier Swagger page rather than as a Postman. It has almost
no logic of its own: what it adds is the ability to load the service's own
enumerations and then reuse them in the fields that take them.

```
python tools/apiclient/run.py
python tools/apiclient/run.py --base-url http://192.168.2.40:5001
```

Qt is not a dependency of the service and must not become one. This tool has its
own hash-pinned lock file, `tools/` is in `.dockerignore`, and `pyproject.toml`
names no Qt package at all. It does mention this directory, in `testpaths`, the
per-file lint ignores and the import-sorting list, none of which installs
anything.

```
pip install --require-hashes -r tools/apiclient/requirements.lock
```

## What it does

**Every endpoint, on one screen.** All twenty-one operations are listed at once,
grouped by what they act on. Selecting one swaps the form beside it. There is no
drill-down and no wizard: the things that would need a quarter of the window to
show properly, a set of markup tokens or a call's full detail, open as dialogs
instead.

**Nothing is hardcoded.** The markup tokens, value tokens, display modes and control
commands all start empty. Press the button beside a set and the client
calls the endpoint for it; the row then says how many arrived, from which
endpoint and when, and a View button opens the list. Only then do the fields that
use that set offer it: before that they are free text, and the button that
inserts a markup token into a message is disabled and says which button to press
first.

That is the point of the design rather than an inconvenience. A client that ships
its own copy of the vocabulary is a client that goes on offering a token for a
year after the service stopped answering it.

Each of the four sets has exactly one endpoint that fills it, so the button
beside a set is unambiguous. All four answer the same shape, `name` and
`description`, and the parsing insists on it: a payload that is a list of
objects but not *this* list of objects is refused rather than quietly producing
a set of empty names, which on screen looks exactly like a healthy one.

**The key boxes offer what is registered, and say when what you typed is not.**
Any form taking a slot key or a variable name has a **Load keys** button beside the
box, which calls `GET /messages` or `GET /variables` and offers what came back. What you had already typed is
kept if it is among them and cleared if it is not, because the list is the answer
to what exists: a key missing from it is one the read would 404 on and the delete
too. Nothing is selected for you either way, so Load followed by Send cannot act
on a slot you never named.

The whitespace around a key is trimmed, both when it is compared with that list
and when Send is pressed, and the box is rewritten to the trimmed key in both
cases. The client trims every path value on its way out regardless, and the
service refuses whitespace in a key at all, so the trimmed key is the one that is
actually used. Showing it in the box means the screen says so, rather than
keeping the spaces as though they had gone somewhere. Case is left alone:
`Porch` and `porch` are different slots.

**A message already on the sign can be loaded back to edit it.** Register or
replace a message carries a **Load From Sign** button under the message caption.
It calls `GET /messages/{key}` for whatever key is in the box above and fills the
form from the answer: the message text with its markup intact, the display mode,
the order and the source. Editing a message that is already up is then reading it
first rather than retyping it from the slot table.

A key with nothing stored under it answers 404, which is shown like any other
failure and changes no field, so a half-written message survives a mistyped key.
Nothing about the failure is special-cased; it is the ordinary response display.

`ttl_seconds` is converted rather than copied, because the service answers in
the other unit: it takes a deadline as seconds from now and reports it as
`expires_at`, the moment itself. The box is filled with the whole seconds left
until that moment, so loading a message that expires in ten minutes and sending
it straight back keeps roughly the deadline it had. Roughly, and not exactly:
the clock runs while the form sits open, so the deadline moves out by however
long the editing took.

The box is left empty for the three cases with no positive number of seconds
ahead to show: a message with no deadline, a timestamp the client could not
read, and one that has already passed. Empty is also what the field means by
"no deadline", which is the right answer for the first and the only available
one for the other two. The response panel shows the timestamp in every case, so
an expired deadline is visible there rather than only implied by an empty box.

**Responses are read, not dumped.** Every shape the service answers with has a
formatter: the health breakdown, the slot table, the alert, the clock, the
enumerations, and a 204 rendered as the success it is rather than as an empty
panel. Anything unrecognised falls back to a labelled walk, so a field added to
the service shows up as a row instead of breaking the view. No raw JSON reaches
the main panel.

**An error is not only a 4xx.** A body that is not JSON at all is one too,
whatever the status above it, and the client treats it as a failure: the status
strip turns red and the full response opens in a dialog. That is how being
pointed at a proxy error page on the right port looks, and reading it as an
absent value would let a formatter announce that the sign is idle on the
strength of it. A tool that coloured by status alone would show it in green.

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

The key is sent as `X-API-Key` on the requests that need it, and it is treated as a
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
| `names.py` | no | the three names this tool answers to, and the QSettings location they build |
| `net.py` | yes | `QNetworkAccessManager`, one call in flight at a time |
| `window.py` | yes | the one screen |
| `dialogs.py` | yes | enumerations, errors, history |
| `app.py` | yes | the command line and `main()` |

The forms are generated from `catalogue.py` rather than written out one by one,
which is what makes "it can call any endpoint" a property of a table rather than
a claim about a window. Hand-written tables drift, so
`tools/apiclient/tests/test_catalogue.py` diffs it against `docs/openapi.json`:
every endpoint in both directions, and for each one the body field names, which
of them are required, and which operations carry a body at all. A route or a
field added to the service fails this tool's tests in the same commit.

What the description cannot supply is the reason the table is written by hand
rather than generated. It declares no enumerations at all: `display_mode` is a
string with a default, because the service validates against `tokens.py` at
request time rather than freezing the vocabulary into the schema. So which field
draws on which set, which response gets which formatter, and which text fields
take markup are all decisions no generator could read out of it.

One thing in the table is not from the schema and is named so it cannot be
mistaken for it. `prefill` is what a field starts out holding. Where the schema
declares a default the two must agree, and a test insists on it; where it
declares none the prefill would be this client's own convenience, and today
there is not one. A test spells that out as an empty set, so offering one has
to be a deliberate change to the assertion.

The HTTP client is Qt's own `QtNetwork`, which is why this tool needs no
dependency the simulator does not already have. There is no requests, no httpx
and no certifi to pin.

Beside the modules are `icon.svg` and `icon.ico`. The first is the drawing, a
paper plane on a blue tile for the tool that sends things, and the one to edit;
the second is what the window loads, holding the drawing at each size Windows
draws an icon at. `scripts/render_icons.py` makes the second from the first,
and its `--check` runs in CI so that an edited drawing cannot be committed
without its rendering. The simulator's icon is the sign itself, amber dots on
black, and the two are deliberately nothing alike so that they are told apart
at a glance on a taskbar.

On Windows the icon would reach the title bar and not the taskbar without one
more step, because the taskbar files a window under its process's executable,
which for a Python program is `python.exe`. `app.py` names the process to the
taskbar before it has a window, as `readerboard.apiclient`, which is what puts
the same icon in both places and gives the client and the simulator a taskbar
button each when they are run together. One thing to know when the drawing
changes: the taskbar keeps the last icon it showed for that name for a while
after the window closes, so a relaunch within a minute or so can show the old
icon on the taskbar and the new one in the title bar. Waiting, or signing out
and back in, clears it.

One call is in flight at a time. That is not worth engineering around: the thing
on the other end drives a single sign down a 9600 baud line behind one lock, so
overlapping calls would tell you less than they appear to.

## Deliberately not here

No saved collections, no environments, no scripting, no response assertions. Each
of those would make this a worse Swagger page and a poor Postman.

## Testing

```
pytest tools/apiclient/tests
```

Runs without Qt, and is collected by the repository's own `pytest` run. Because
of that, nothing in it builds a window, so CI additionally constructs one
offscreen in the job that already has Qt installed. A signal connected to an
attribute that does not exist yet raises on construction and nowhere else, and
that is otherwise nobody's job to notice.

Type checking is a separate invocation, because the root `mypy` config names
only the service:

```
MYPYPATH=tools/apiclient mypy tools/apiclient/apiclient
```

CI runs it in the lint job, after installing the lock file above, for the reason
it runs the simulator's: an invocation only ever run by hand rots without
anybody hearing about it.
