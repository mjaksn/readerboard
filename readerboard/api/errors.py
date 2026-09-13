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

from readerboard.icons import IconError
from readerboard.protocol.frames import ProtocolError
from readerboard.protocol.markup import MarkupError
from readerboard.protocol.replies import ReplyError
from readerboard.services import commands
from readerboard.services.alerts import AlertTooLong
from readerboard.services.registry import (
    IconsDisabled,
    MessageTooLong,
    PicturePoolFull,
    UnknownSlot,
    UnknownVariable,
    VariableInUse,
    VariablesDisabled,
    VariableTooLong,
)
from readerboard.sign.layout import LayoutFull
from readerboard.sign.pool import PoolTooLarge
from readerboard.transport.base import TransportError

STATUS_FOR_ERROR: tuple[tuple[type[Exception], int], ...] = (
    (MarkupError, status.HTTP_400_BAD_REQUEST),
    (ProtocolError, status.HTTP_400_BAD_REQUEST),
    (MessageTooLong, status.HTTP_400_BAD_REQUEST),
    (VariableTooLong, status.HTTP_400_BAD_REQUEST),
    (VariablesDisabled, status.HTTP_400_BAD_REQUEST),
    (IconsDisabled, status.HTTP_400_BAD_REQUEST),
    # An icon nobody has, or a tint on one drawn in fixed colours. The library
    # raises these rather than the renderer, because the renderer is not told
    # which icons exist; mapping the one base they share is what keeps a
    # misspelled icon name a 400 rather than a 500.
    (IconError, status.HTTP_400_BAD_REQUEST),
    (AlertTooLong, status.HTTP_400_BAD_REQUEST),
    (commands.UnknownCommand, status.HTTP_400_BAD_REQUEST),
    (commands.BadParameter, status.HTTP_400_BAD_REQUEST),
    (UnknownSlot, status.HTTP_404_NOT_FOUND),
    (UnknownVariable, status.HTTP_404_NOT_FOUND),
    (LayoutFull, status.HTTP_409_CONFLICT),
    # Only POST /sign/reboot can raise this: the sign has less memory than the
    # configuration needs, found by asking it just before the reboot would have
    # erased it. The state of the hardware rather than anything wrong with the
    # request, which is what makes it a conflict, and nothing was written.
    (PoolTooLarge, status.HTTP_409_CONFLICT),
    # Deleting a variable a message still calls would leave that message
    # calling a file the next variable could be given. Not the caller's
    # request being malformed, which is what makes it a conflict.
    (VariableInUse, status.HTTP_409_CONFLICT),
    # Every picture file is holding an icon something still calls. Like a full
    # slot pool and unlike a bad icon name, this is the state of the sign rather
    # than anything wrong with the request.
    (PicturePoolFull, status.HTTP_409_CONFLICT),
    (TransportError, status.HTTP_503_SERVICE_UNAVAILABLE),
    # A sign that answers with something unreadable is as unusable as one
    # that does not answer, and neither is the caller's doing. The route
    # lets this propagate like any other; deciding the code here rather than
    # in the route body is what keeps that decision in one place.
    (ReplyError, status.HTTP_503_SERVICE_UNAVAILABLE),
)
