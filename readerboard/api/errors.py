"""What each of the service's own exceptions means as a status code.

One table, read twice. The ``/v2`` surface lets these propagate and an exception
handler turns each into a status code with FastAPI's usual ``detail`` body. The
simple surface catches them instead, because it answers in its own shape
whatever happened, and it has to give the same answer to the same question. Two
tables would be one table and a copy of it that goes stale, and the copy would
go stale silently: nothing fails when two surfaces disagree about what a broken
cable means.

Add an exception here and both surfaces learn about it together.
"""

from __future__ import annotations

from fastapi import status

from readerboard.protocol.frames import ProtocolError
from readerboard.protocol.markup import MarkupError
from readerboard.services import commands
from readerboard.services.alerts import AlertTooLong
from readerboard.services.registry import MessageTooLong, UnknownSlot
from readerboard.sign.layout import LayoutFull
from readerboard.transport.base import TransportError

# Most specific first. FastAPI dispatches a handler by walking the exception's
# own class hierarchy, so a subclass registered after its parent would still
# reach the right one; the linear scan in status_for would not. Keeping the
# order means the two ways of reading this table cannot disagree.
STATUS_FOR_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (MarkupError, status.HTTP_400_BAD_REQUEST),
    (ProtocolError, status.HTTP_400_BAD_REQUEST),
    (MessageTooLong, status.HTTP_400_BAD_REQUEST),
    (AlertTooLong, status.HTTP_400_BAD_REQUEST),
    (commands.UnknownCommand, status.HTTP_400_BAD_REQUEST),
    (commands.BadParameter, status.HTTP_400_BAD_REQUEST),
    (UnknownSlot, status.HTTP_404_NOT_FOUND),
    (LayoutFull, status.HTTP_409_CONFLICT),
    (TransportError, status.HTTP_503_SERVICE_UNAVAILABLE),
)


def status_for(err: Exception) -> int:
    """Return the status code an exception means, or 500 when it means nothing.

    A 500 is the honest answer for an exception this service has never named:
    it did not plan for this, so it cannot tell the caller whose fault it was.
    """
    for kind, code in STATUS_FOR_ERROR:
        if isinstance(err, kind):
            return code
    return status.HTTP_500_INTERNAL_SERVER_ERROR
