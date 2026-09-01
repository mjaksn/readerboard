"""What each of the service's own exceptions means as a status code.

One table, and one reader of it. Routes let these propagate; an exception
handler registered from this table turns each into a status code with FastAPI's
usual ``detail`` body. An exception this table does not name reaches nobody's
handler and is a 500, which is the honest answer for a failure the service never
planned for: it cannot say whose fault it was.

Add an exception here and the handler for it is registered with it.
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
