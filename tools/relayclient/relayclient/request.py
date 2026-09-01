"""Turning what is on screen into the request that goes out.

This deliberately validates almost nothing. The tool exists to exercise the
service, and a client that rejects a bad body itself is a client that cannot show
you what the service does with one: the 400 for a message the sign cannot render,
the 422 for a field of the wrong type, the 409 when the pool is full. All of
those are worth being able to provoke.

So the only things refused here are the ones that would stop a request being
formed at all: a path parameter with nothing in it, which would collapse the URL,
and a base URL that is not a URL. A number field whose text is not a number is
sent as the text, so the service answers for it.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from relayclient.catalogue import Operation

API_KEY_HEADER = "X-API-Key"

# What stands in for the key wherever it would otherwise be written down. Here
# rather than in history.py because this is where the substitution happens, and
# two spellings of it in two modules is one more than can stay in step.
REDACTED = "<redacted>"

# What the curl command refers to rather than the key itself, everywhere the key
# would otherwise be written down.
API_KEY_VARIABLE = "READERBOARD_API_KEY"


class InvalidRequest(Exception):
    """Raised when the inputs cannot be turned into a request at all."""


@dataclass(frozen=True, slots=True)
class Prepared:
    """A request ready to be sent, and readable enough to show in a history pane."""

    operation_id: str
    method: str
    url: str
    path: str
    headers: dict[str, str]
    body: str | None

    @property
    def origin(self) -> str:
        """Return the base URL this went to, which is not always the one on screen now."""
        return self.url[: len(self.url) - len(self.path)]

    @property
    def redacted_headers(self) -> dict[str, str]:
        """Return the headers with the API key replaced rather than carried around."""
        return {
            name: (REDACTED if name.lower() == API_KEY_HEADER.lower() else value)
            for name, value in self.headers.items()
        }


def normalise_base_url(base_url: str) -> str:
    """Return the base URL without a trailing slash, refusing what is not a URL."""
    trimmed = base_url.strip().rstrip("/")
    if not trimmed:
        raise InvalidRequest("the base URL is empty")
    parts = urlsplit(trimmed)
    if parts.scheme not in ("http", "https"):
        raise InvalidRequest(
            "the base URL needs an http:// or https:// scheme, not %r" % (parts.scheme or "none")
        )
    if not parts.netloc:
        raise InvalidRequest("the base URL has no host")
    return trimmed


def fill_path(operation: Operation, values: dict[str, str]) -> str:
    """Substitute the path parameters, percent-encoding each one."""
    path = operation.path
    for item in operation.path_inputs:
        raw = (values.get(item.name) or "").strip()
        if not raw:
            raise InvalidRequest("%s is part of the path and cannot be empty" % item.label)
        path = path.replace("{%s}" % item.name, quote(raw, safe=""))
    return path


def coerce(kind: str, text: str) -> object:
    """Return the text as the kind of value the field wants, or as text if it will not.

    ``nan``, ``inf`` and ``-inf`` parse as floats and are deliberately refused,
    because there is no way to write them in JSON: :func:`json.dumps` emits a
    bare ``NaN``, which the specification does not allow. Sending them as text
    keeps the promise this module makes, that a number field holding something
    that is not a number is answered for by the service, field by field, rather
    than turned into a document it cannot read.
    """
    if kind == "int":
        try:
            return int(text)
        except ValueError:
            return text
    if kind == "float":
        try:
            value = float(text)
        except ValueError:
            return text
        return value if math.isfinite(value) else text
    return text


def build_body(operation: Operation, values: dict[str, str]) -> dict[str, object] | None:
    """Assemble the JSON body, leaving out the optional fields nobody filled in.

    An omitted optional field is not the same as one sent as null, and the
    service's models say so: ``extra="forbid"`` with defaults means leaving a
    field out is how you ask for the default.
    """
    if not operation.body:
        return None

    body: dict[str, object] = {}
    for item in operation.body:
        raw = values.get(item.name)
        text = "" if raw is None else str(raw)
        # Empty, not blank. A field holding a single space was typed that way on
        # purpose, and rewriting it to "" or dropping it answers a question the
        # caller did not ask. Only a field nobody touched is left out.
        if not text:
            if item.required:
                # Sent empty, so the service answers for it rather than the
                # client deciding what an empty required field means. The
                # prefill's job is to start a field off, not to put back a value
                # somebody cleared on purpose to see what would happen.
                body[item.name] = ""
            continue
        body[item.name] = coerce(item.kind, text)
    return body


def build(
    operation: Operation,
    base_url: str,
    *,
    path_values: dict[str, str] | None = None,
    body_values: dict[str, str] | None = None,
    api_key: str = "",
) -> Prepared:
    """Turn an operation and the values on screen into a request ready to send."""
    root = normalise_base_url(base_url)
    path = fill_path(operation, path_values or {})
    body = build_body(operation, body_values or {})

    headers: dict[str, str] = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if operation.needs_key and api_key:
        headers[API_KEY_HEADER] = api_key

    return Prepared(
        operation_id=operation.id,
        method=operation.method,
        url=root + path,
        path=path,
        headers=headers,
        body=None if body is None else json.dumps(body, indent=2, ensure_ascii=False),
    )


def _shell_quote(value: str) -> str:
    """Return the value single-quoted for a POSIX shell."""
    return "'%s'" % value.replace("'", "'\\''")


def as_curl(
    method: str,
    url: str,
    headers: dict[str, str],
    body: str | None,
    *,
    key_variable: str = API_KEY_VARIABLE,
) -> str:
    """Render a call as a curl command that can be pasted somewhere useful.

    The API key is never written into the command. It comes out as a reference to
    an environment variable, so the result is safe to paste into a script, a
    ticket or a message to somebody else, which is most of the reason to want it.
    """
    parts = ["curl -X %s %s" % (method, _shell_quote(url))]
    for name, value in headers.items():
        if name.lower() == API_KEY_HEADER.lower():
            # Double quotes so the shell expands the variable, which single
            # quotes would not.
            parts.append('-H "%s: $%s"' % (name, key_variable))
        else:
            parts.append("-H %s" % _shell_quote("%s: %s" % (name, value)))
    if body is not None:
        parts.append("-d %s" % _shell_quote(body))
    return " \\\n  ".join(parts)
