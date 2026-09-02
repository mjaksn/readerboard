"""Comparing the surface this client knows against the one it is pointed at.

The catalogue is built from the description in this checkout. The service on the
other end of the wire may be older: a Pi that has not been updated still answers
the surface it shipped with. Without this, pointing the client at it offers
endpoints that are not there, and the way you find out is a 404 that looks like a
bug in the client.

So the client asks the service to describe itself, once per address, and says
plainly when the two disagree. This is not a call the user made, so it stays out
of the history and out of the catalogue: ``/openapi.json`` is not part of the
described surface, and adding it to the table would make the endpoint diff in
``tools/apiclient/tests/test_catalogue.py`` fail for a path the document never
lists.
"""

from __future__ import annotations

from dataclasses import dataclass

from apiclient.catalogue import Operation

DESCRIPTION_PATH = "/openapi.json"

# A path item may carry `parameters`, `summary`, `description`, `servers` and
# `$ref` beside its operations. FastAPI emits none of them today, but this code
# exists to read other services' descriptions, and one that did would otherwise
# come back as a surface full of endpoints named PARAMETERS.
HTTP_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


class UnreadableDescription(Exception):
    """Raised when what came back is not an OpenAPI description."""


def paths_in(document: object) -> set[tuple[str, str]]:
    """Return every (method, path) a description declares."""
    if not isinstance(document, dict):
        raise UnreadableDescription(
            "expected an object, got %s" % type(document).__name__
        )
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise UnreadableDescription("the description has no paths object")

    found: set[tuple[str, str]] = set()
    for path, operations in paths.items():
        if not isinstance(operations, dict):
            continue
        for method in operations:
            if str(method).lower() in HTTP_METHODS:
                found.add((str(method).upper(), str(path)))
    return found


def version_in(document: object) -> str:
    """Return the version the description claims, or a blank when it claims none."""
    if isinstance(document, dict):
        info = document.get("info")
        if isinstance(info, dict):
            return str(info.get("version", ""))
    return ""


@dataclass(frozen=True, slots=True)
class Skew:
    """How the service's surface differs from the one this client was built for."""

    version: str
    uncallable: tuple[tuple[str, str], ...]
    unknown: tuple[tuple[str, str], ...]

    @property
    def matches(self) -> bool:
        """Whether the two surfaces agree exactly."""
        return not self.uncallable and not self.unknown

    def summary(self) -> str:
        """Return the one line the window shows beside the address."""
        named = "service %s" % self.version if self.version else "the service"
        if self.matches:
            return "%s, surface matches" % named
        parts = []
        if self.unknown:
            parts.append(
                "%d this client offers that it does not have" % len(self.unknown)
            )
        if self.uncallable:
            parts.append("%d it has that this client cannot call" % len(self.uncallable))
        return "%s: %s" % (named, ", ".join(parts))

    def detail(self) -> str:
        """Return the full account, for the dialog and the tooltip."""
        lines = [self.summary(), ""]
        if self.unknown:
            lines.append(
                "This client offers these, and the service does not have them. "
                "Calling one will answer 404:"
            )
            lines.extend("    %s %s" % (method, path) for method, path in self.unknown)
            lines.append("")
        if self.uncallable:
            lines.append(
                "The service has these, and this client has no form for them. "
                "Its catalogue is older than the service:"
            )
            lines.extend("    %s %s" % (method, path) for method, path in self.uncallable)
            lines.append("")
        if self.matches:
            lines.append("Everything this client offers is there, and nothing is missing.")
        else:
            lines.append(
                "Nothing is broken by this. Every call still goes out, and the ones "
                "listed above are the only ones that will surprise you."
            )
        return "\n".join(lines).strip()


def compare(document: object, operations: tuple[Operation, ...]) -> Skew:
    """Compare a service's own description against what this client can call."""
    described = paths_in(document)
    catalogued = {(operation.method, operation.path) for operation in operations}
    return Skew(
        version=version_in(document),
        uncallable=tuple(sorted(described - catalogued)),
        unknown=tuple(sorted(catalogued - described)),
    )
