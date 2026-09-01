"""Every call this run has made, in the shape a network tab shows them.

The history lives for the run and goes when the window closes. That is a
deliberate limit rather than an unfinished feature: nothing here needs a file
format, and nothing anybody sent ends up on disk.

The API key is redacted when the record is made, not when it is displayed. A
record that never holds the key cannot leak it into a copied curl command, a
screenshot or a bug report, and there is no display path left to forget about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from relayclient import request as request_module

REDACTED = "<redacted>"


@dataclass(frozen=True, slots=True)
class Record:
    """One call, from the request that went out to whatever came back."""

    index: int
    started_at: datetime
    operation_id: str
    summary: str
    method: str
    url: str
    path: str
    request_headers: dict[str, str]
    request_body: str | None
    status: int
    reason: str
    ok: bool
    duration_ms: int
    response_headers: dict[str, str]
    response_body: str

    @property
    def stamp(self) -> str:
        """Return the time of the call, to the second."""
        return self.started_at.strftime("%H:%M:%S")

    @property
    def outcome(self) -> str:
        """Return the short status column the list shows."""
        if self.status == 0:
            return "failed"
        return str(self.status)

    def as_curl(self) -> str:
        """Render this call as a curl command, with the key left as a variable."""
        return request_module.as_curl(
            self.method,
            self.url,
            self.request_headers,
            self.request_body,
        )


@dataclass
class History:
    """The calls made this run, oldest first."""

    records: list[Record] = field(default_factory=list)

    def append(
        self,
        *,
        prepared: request_module.Prepared,
        summary: str,
        started_at: datetime,
        status: int,
        reason: str,
        ok: bool,
        duration_ms: int,
        response_headers: dict[str, str],
        response_body: str,
    ) -> Record:
        """Record a completed call and return it.

        The headers stored are :attr:`Prepared.redacted_headers`, so the key is
        gone before the record exists.
        """
        record = Record(
            index=len(self.records) + 1,
            started_at=started_at,
            operation_id=prepared.operation_id,
            summary=summary,
            method=prepared.method,
            url=prepared.url,
            path=prepared.path,
            request_headers=dict(prepared.redacted_headers),
            request_body=prepared.body,
            status=status,
            reason=reason,
            ok=ok,
            duration_ms=duration_ms,
            response_headers=dict(response_headers),
            response_body=response_body,
        )
        self.records.append(record)
        return record

    def __len__(self) -> int:
        """Return how many calls have been made this run."""
        return len(self.records)

    def latest_first(self) -> list[Record]:
        """Return the records newest first, which is how the list reads."""
        return list(reversed(self.records))
