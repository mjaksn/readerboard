"""Turning a response into something a person can read at a glance.

No raw JSON reaches the main panel. Every shape the service answers with has a
formatter here, and anything unrecognised falls back to a readable walk rather
than to a dump, so a new field added to the service shows up as a labelled row
instead of breaking the view.

The one thing worth knowing before changing anything here: **an error is not
only a 4xx**. A body that is not JSON at all is one too, whatever the status
above it, because every formatter below reads a missing value as a value and
would state the absence as a fact. That is what being pointed at a proxy's own
error page looks like. :func:`is_error` is the whole of that rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape

from apiclient.catalogue import Operation


@dataclass(frozen=True, slots=True)
class Row:
    """One labelled value."""

    label: str
    value: str
    hint: str = ""


@dataclass(frozen=True, slots=True)
class Section:
    """A group of labelled values under an optional heading."""

    title: str
    rows: tuple[Row, ...]


@dataclass(frozen=True, slots=True)
class Table:
    """A grid, for the responses that are lists of the same shape."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Note:
    """A sentence on its own, for the responses that have nothing to tabulate."""

    text: str


Block = Section | Table | Note


@dataclass(slots=True)
class Rendered:
    """Everything the response panel needs, and the text the error dialog shows."""

    headline: str
    ok: bool
    blocks: list[Block] = field(default_factory=list)
    detail: str = ""


# What the codes this service uses actually mean here, in its own terms. Taken
# from the docstring at the top of readerboard/api/routes.py and the OpenAPI
# description in readerboard/api/app.py. What decides them is the table in
# readerboard/api/errors.py, which maps each of the service's own exceptions
# to a code; the 401 and the no-key 503 are raised earlier still, by
# require_api_key in readerboard/api/deps.py.
STATUS_MEANING = {
    200: "the call succeeded",
    201: "created",
    204: "the service accepted it and had nothing to send back",
    400: "the sign cannot render what was sent",
    401: "the API key was missing or wrong",
    403: "refused",
    404: "no slot by that name, or no such route on this service",
    409: "the slot pool is full",
    422: "the body was not the shape the endpoint declares",
    500: "the service raised something it did not expect",
    503: "the sign is unreachable, or no API key is configured at all",
}


class Unreadable:
    """A body that is not JSON at all, which is not the same as a body of null.

    ``GET /alerts`` answers a literal ``null`` when nothing is holding the
    sign, and :func:`_alert` turns that into a sentence saying so. Collapsing
    both onto ``None`` would print that sentence, in green, over a proxy error
    page or a captive portal: the same mistake as colouring by status code,
    moved one layer down. So the unreadable body is this instead.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        """Name it, since it would otherwise appear as an anonymous object."""
        return "<unreadable body>"


UNREADABLE = Unreadable()


def parse_body(text: str) -> object | None:
    """Return the body as parsed JSON.

    ``None`` for an empty body and for a literal ``null``, which the service
    does send. :data:`UNREADABLE` for anything that is not JSON at all, which it
    never sends.
    """
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed: object = json.loads(stripped)
    except ValueError:
        return UNREADABLE
    return parsed


def is_error(status: int, payload: object | None) -> bool:
    """Whether this response should be read as a failure.

    Two ways to fail. The usual one is a status code of 400 or above, or no
    response at all. The other is a body that is not JSON: something answered,
    but every formatter below reads a missing value as a value and would state
    the absence as a fact.

    Both are decided here rather than in :func:`render`, because the window asks
    this question separately to colour its status strip. A rule that lived in
    the renderer would turn the panel red and leave the strip green.
    """
    return status == 0 or status >= 400 or payload is UNREADABLE


def when(value: object) -> str:
    """Render a timestamp the way a person reads one, with how long ago it was."""
    if value is None:
        return "never"
    text = str(value)
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return text

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    seconds = (moment - now).total_seconds()
    stamp = moment.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return "%s (%s)" % (stamp, _relative(seconds))


def _relative(seconds: float) -> str:
    """Return a rough 'in 4 minutes' or '12 seconds ago' for a second count."""
    ahead = seconds >= 0
    seconds = abs(seconds)
    if seconds < 1:
        return "now"
    for size, name in ((86400.0, "day"), (3600.0, "hour"), (60.0, "minute"), (1.0, "second")):
        if seconds >= size:
            count = int(seconds // size)
            unit = name if count == 1 else name + "s"
            return ("in %d %s" % (count, unit)) if ahead else ("%d %s ago" % (count, unit))
    return "now"


def yes_no(value: object) -> str:
    """Render a boolean as a word rather than as True or False."""
    return "yes" if value else "no"


def headline_for(status: int, reason: str, *, ok: bool = True) -> str:
    """Return the status line as it appears above the response.

    ``ok`` is not decoration. A body that is not JSON can arrive under a 200,
    and ``STATUS_MEANING`` reads that as "the call succeeded", so a headline
    built from the status alone would announce success at the top of a response
    this module has just decided is a failure. That is precisely the mistake the
    module exists to prevent, and it would be made in its own first line.
    """
    if status == 0:
        return "No response: %s" % (reason or "the request did not complete")
    label = "%d %s" % (status, reason) if reason else str(status)
    if not ok and status < 400:
        return "%s, but the body is not JSON and cannot be read" % label
    meaning = STATUS_MEANING.get(status)
    return "%s, %s" % (label, meaning) if meaning else label


# ===========================================================================
# The error path
# ===========================================================================


def render_error(status: int, reason: str, payload: object | None, raw: str) -> Rendered:
    """Render a failure, whichever of the three shapes it arrived in."""
    rendered = Rendered(headline=headline_for(status, reason, ok=False), ok=False)

    if payload is UNREADABLE:
        # Said on a 4xx as well as on a 200, deliberately. This service reports
        # its own failures as JSON, so a body that is not JSON says the same
        # thing whatever the code above it: the answer came from somewhere else.
        rendered.blocks.append(
            Note(
                "The body is not JSON, so none of it has been read as a value. "
                "Whatever answered on this address may not be this service."
            )
        )
        rendered.blocks.append(Note(raw.strip()))
        rendered.detail = raw
        return rendered

    if isinstance(payload, dict) and "detail" in payload:
        detail = payload["detail"]
        if isinstance(detail, list):
            rows: list[tuple[str, ...]] = []
            for item in detail:
                if isinstance(item, dict):
                    location = item.get("loc", [])
                    where = " -> ".join(str(part) for part in location) if location else "body"
                    rows.append((where, str(item.get("msg", "")), str(item.get("type", ""))))
                else:
                    rows.append(("", str(item), ""))
            rendered.blocks.append(
                Table(
                    title="What the service objected to",
                    columns=("where", "what", "kind"),
                    rows=tuple(rows),
                )
            )
        else:
            rendered.blocks.append(Note(str(detail)))
        rendered.detail = raw
        return rendered

    if raw.strip():
        rendered.blocks.append(Note(raw.strip()))
    else:
        rendered.blocks.append(Note("The service sent no body with this."))
    rendered.detail = raw
    return rendered


# ===========================================================================
# The shapes the service answers with
# ===========================================================================


def _health(payload: object) -> list[Block]:
    """Render GET /health."""
    if not isinstance(payload, dict):
        return _generic(payload)
    raw_link = payload.get("link")
    link: dict[str, object] = raw_link if isinstance(raw_link, dict) else {}
    return [
        Section(
            title="Service",
            rows=(
                Row("status", str(payload.get("status", ""))),
                Row("version", str(payload.get("version", ""))),
                Row("alert active", yes_no(payload.get("alert_active"))),
                Row("clock last synced", when(payload.get("clock_last_synced_at"))),
            ),
        ),
        Section(
            title="Link to the sign",
            rows=(
                Row("url", str(link.get("url", ""))),
                Row("connected", yes_no(link.get("connected"))),
                Row("last write", when(link.get("last_write_at"))),
                Row("last error", str(link.get("last_error") or "none")),
                Row("writes", str(link.get("writes", 0))),
                Row(
                    "suppressed writes",
                    str(link.get("suppressed_writes", 0)),
                    "skipped because the sign already held those exact bytes",
                ),
            ),
        ),
        Section(
            title="Messages",
            rows=(
                Row(
                    "slots used",
                    "%s of %s" % (payload.get("slots_used", 0), payload.get("slots_total", 0)),
                ),
                Row(
                    "sign in sync",
                    yes_no(payload.get("sign_in_sync")),
                    "no means something was accepted but has not reached the sign",
                ),
            ),
        ),
    ]


_SLOT_COLUMNS = ("key", "file", "message", "mode", "position", "order", "source", "expires")


def _slot_row(slot: dict[str, object]) -> tuple[str, ...]:
    """Render one slot as a table row."""
    expires = slot.get("expires_at")
    return (
        str(slot.get("key", "")),
        str(slot.get("label", "")),
        str(slot.get("message", "")),
        str(slot.get("display_mode", "")),
        str(slot.get("position", "")),
        str(slot.get("order", "")),
        str(slot.get("source") or ""),
        when(expires) if expires else "never",
    )


def _slots(payload: object) -> list[Block]:
    """Render the list of registered slots."""
    if not isinstance(payload, list):
        return _generic(payload)
    if not payload:
        return [Note("No messages are registered. The sign is showing nothing.")]
    rows = tuple(_slot_row(item) for item in payload if isinstance(item, dict))
    return [
        Note(
            "%d message%s sharing the sign, in the order it plays them."
            % (len(rows), "" if len(rows) == 1 else "s")
        ),
        Table(title="", columns=_SLOT_COLUMNS, rows=rows),
    ]


def _slot(payload: object) -> list[Block]:
    """Render one slot."""
    if not isinstance(payload, dict):
        return _generic(payload)
    return [
        Section(
            title="",
            rows=(
                Row("key", str(payload.get("key", ""))),
                Row("sign file", str(payload.get("label", "")), "the file on the sign it occupies"),
                Row("message", str(payload.get("message", ""))),
                Row("display mode", str(payload.get("display_mode", ""))),
                Row("position", str(payload.get("position", ""))),
                Row("order", str(payload.get("order", ""))),
                Row("source", str(payload.get("source") or "not recorded")),
                Row(
                    "expires",
                    when(payload["expires_at"]) if payload.get("expires_at") else "never",
                ),
                Row("updated", when(payload.get("updated_at"))),
            ),
        )
    ]


def _alert(payload: object) -> list[Block]:
    """Render the alert, which is null when the sign is rotating normally."""
    if payload is None:
        return [Note("No alert is holding the sign. It is rotating normally.")]
    if not isinstance(payload, dict):
        return _generic(payload)
    return [
        Section(
            title="",
            rows=(
                Row("message", str(payload.get("message", ""))),
                Row("display mode", str(payload.get("display_mode", ""))),
                Row("position", str(payload.get("position", ""))),
                Row("started", when(payload.get("started_at"))),
                Row(
                    "expires",
                    when(payload["expires_at"])
                    if payload.get("expires_at")
                    else "never, it holds the sign until released",
                ),
            ),
        ),
        Note("While this is up, every other message on the sign is suppressed."),
    ]


def _clock(payload: object) -> list[Block]:
    """Render the clock sync result."""
    if not isinstance(payload, dict):
        return _generic(payload)
    return [
        Section(title="", rows=(Row("synced at", when(payload.get("synced_at"))),)),
        Note("The sign was told this time in its configured zone."),
    ]


def _tokens(payload: object) -> list[Block]:
    """Render an enumeration as the name and description table it is.

    ``name`` is read here and in :func:`apiclient.enums.parse` alike. Reading a
    different field in either place would put a green table and a "that did not
    look like an enumeration" warning on screen for the same response.
    """
    if not isinstance(payload, list):
        return _generic(payload)
    rows: list[tuple[str, ...]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append((str(item.get("name", "")), str(item.get("description", ""))))
    return [
        Note("%d entr%s." % (len(rows), "y" if len(rows) == 1 else "ies")),
        Table(title="", columns=("name", "description"), rows=tuple(rows)),
    ]


def _empty(payload: object) -> list[Block]:
    """Render a response that is meant to have no body."""
    if payload is None:
        return [Note("The service accepted it and returned no content, which is success here.")]
    return _generic(payload)


def _generic(payload: object, prefix: str = "") -> list[Block]:
    """Walk anything unrecognised into labelled rows rather than dumping it."""
    if payload is None:
        return [Note("The service sent no body.")]
    if isinstance(payload, dict):
        rows = tuple(
            Row(prefix + str(name), _scalar(value)) for name, value in payload.items()
        )
        return [Section(title="", rows=rows)]
    if isinstance(payload, list):
        rows = tuple(Row("[%d]" % index, _scalar(item)) for index, item in enumerate(payload))
        return [Section(title="", rows=rows)]
    return [Note(_scalar(payload))]


def _scalar(value: object) -> str:
    """Render a leaf value, compacting a nested one rather than dropping it."""
    if isinstance(value, bool):
        return yes_no(value)
    if value is None:
        return "none"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


_FORMATTERS = {
    "health": _health,
    "slots": _slots,
    "slot": _slot,
    "alert": _alert,
    "clock": _clock,
    "tokens": _tokens,
    "empty": _empty,
    "generic": _generic,
}


def render(operation: Operation, status: int, reason: str, raw: str) -> Rendered:
    """Render a whole response, choosing the failure path when it is one."""
    payload = parse_body(raw)

    if is_error(status, payload):
        return render_error(status, reason, payload, raw)

    rendered = Rendered(headline=headline_for(status, reason), ok=True)
    rendered.blocks.extend(_FORMATTERS.get(operation.formatter, _generic)(payload))
    return rendered


# ===========================================================================
# The same thing again as HTML, which is what the panel actually shows
# ===========================================================================

# Kept beside the formatters rather than in the window, because it is pure and
# so it can be tested. The response panel and the history details pane both
# render through here, which is why the two never drift apart.

@dataclass(frozen=True, slots=True)
class Theme:
    """The colours the rendered HTML uses, so a dark desktop is not grey on dark.

    QTextBrowser paints its own background from the widget palette, so a
    formatter that hardcodes ink for a near-white ground produces mid-grey text
    on a dark panel for anybody running a dark system theme. The window picks
    the pair; everything here just reads them.
    """

    ink: str
    muted: str
    rule: str
    faint_rule: str
    ok: str
    bad: str


LIGHT = Theme(
    ink="#1b1917",
    muted="#7a746a",
    rule="#d8d2c6",
    faint_rule="#eee8dc",
    ok="#0f7d5c",
    bad="#c2410c",
)

DARK = Theme(
    ink="#f2eee6",
    muted="#a79f91",
    rule="#4c463c",
    faint_rule="#332f28",
    ok="#43bf95",
    bad="#e08a4f",
)

# Kept for the callers that want the accent without a whole theme.
OK_COLOUR = LIGHT.ok
BAD_COLOUR = LIGHT.bad


def _rows_html(rows: tuple[Row, ...], theme: Theme) -> str:
    """Render labelled values as a two column table."""
    cells = []
    for row in rows:
        hint = (
            "<div style='color:%s;font-size:11px'>%s</div>" % (theme.muted, escape(row.hint))
            if row.hint
            else ""
        )
        cells.append(
            "<tr>"
            "<td style='padding:3px 14px 3px 0;color:%s;vertical-align:top;"
            "white-space:nowrap'>%s</td>"
            "<td style='padding:3px 0;vertical-align:top'>%s%s</td>"
            "</tr>" % (theme.muted, escape(row.label), escape(row.value) or "&#183;", hint)
        )
    return "<table style='border-collapse:collapse'>%s</table>" % "".join(cells)


def _table_html(block: Table, theme: Theme) -> str:
    """Render a grid."""
    head = "".join(
        "<th style='text-align:left;padding:4px 14px 4px 0;color:%s;"
        "font-weight:normal;border-bottom:1px solid %s;white-space:nowrap'>%s</th>"
        % (theme.muted, theme.rule, escape(column))
        for column in block.columns
    )
    body = []
    for row in block.rows:
        cells = "".join(
            "<td style='padding:4px 14px 4px 0;vertical-align:top;"
            "border-bottom:1px solid %s'>%s</td>" % (theme.faint_rule, escape(cell) or "&#183;")
            for cell in row
        )
        body.append("<tr>%s</tr>" % cells)
    title = (
        "<div style='margin:10px 0 4px;color:%s'>%s</div>" % (theme.muted, escape(block.title))
        if block.title
        else ""
    )
    return "%s<table style='border-collapse:collapse;width:100%%'><tr>%s</tr>%s</table>" % (
        title,
        head,
        "".join(body),
    )


def as_html(rendered: Rendered, theme: Theme = LIGHT) -> str:
    """Render a response as the HTML the panel displays.

    Never a JSON dump. The raw body is available from the history pane, which is
    a network tab and would be useless without it, but the main panel shows only
    what the formatters made of it.
    """
    colour = theme.ok if rendered.ok else theme.bad
    parts = [
        "<div style='font-family:sans-serif;font-size:13px;color:%s'>" % theme.ink,
        "<div style='color:%s;font-weight:600;margin-bottom:10px'>%s</div>"
        % (colour, escape(rendered.headline)),
    ]

    for block in rendered.blocks:
        if isinstance(block, Note):
            parts.append(
                "<div style='margin:8px 0;color:%s'>%s</div>" % (theme.muted, escape(block.text))
            )
        elif isinstance(block, Section):
            if block.title:
                parts.append(
                    "<div style='margin:12px 0 4px;font-weight:600'>%s</div>"
                    % escape(block.title)
                )
            parts.append(_rows_html(block.rows, theme))
        else:
            parts.append(_table_html(block, theme))

    parts.append("</div>")
    return "".join(parts)
