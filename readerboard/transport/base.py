"""What the rest of the service is allowed to assume about the link to the sign."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(RuntimeError):
    """The bytes did not reach the sign.

    This is the one failure the API turns into a 503. The old server reported it
    as HTTP 200 with an error string in the body, which meant a dead serial link
    looked exactly like success to Home Assistant.
    """


@runtime_checkable
class Transport(Protocol):
    """A one-way byte pipe to the sign.

    Implementations are synchronous and are expected to block. The controller
    runs them on a worker thread, so nothing here needs to know about asyncio.
    """

    @property
    def is_open(self) -> bool:
        """Whether the link is currently up."""
        ...

    @property
    def description(self) -> str:
        """A short human readable name for the link, safe to put in a log line."""
        ...

    def ensure_open(self) -> None:
        """Open the link if it is down.

        Raises :class:`TransportError` if it cannot be opened, including when a
        backoff window means it is not yet worth trying.
        """
        ...

    def write(self, data: bytes) -> None:
        """Send one complete transmission.

        Raises :class:`TransportError` if it could not be sent.
        """
        ...

    def close(self) -> None:
        """Close the link. Closing an already closed link does nothing."""
        ...
