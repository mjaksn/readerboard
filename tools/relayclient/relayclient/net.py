"""Moving the bytes, and nothing else.

The transport is Qt's own, so this tool needs no dependency the sign simulator
does not already have. Everything interesting happens before a request gets here
and after a response leaves, in modules that import no Qt and are tested where
Qt is not installed.

One call is in flight at a time. That is not a limitation worth engineering
around: the thing on the other end drives a single sign down a 9600 baud line
behind one lock, so overlapping calls would tell you less than they appear to.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from relayclient.request import Prepared


@dataclass(frozen=True, slots=True)
class Completed:
    """What came back, or what stopped it coming back."""

    prepared: Prepared
    status: int
    reason: str
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    duration_ms: int = 0


class Caller(QObject):
    """Sends one prepared request at a time and reports what happened."""

    completed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        """Build the caller with its own network manager."""
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._prepared: Prepared | None = None
        self._started = 0.0

    @property
    def busy(self) -> bool:
        """Whether a call is still in flight."""
        return self._reply is not None

    def send(self, prepared: Prepared) -> None:
        """Send a prepared request, ignoring the call if one is already running."""
        if self._reply is not None:
            return

        request = QNetworkRequest(QUrl(prepared.url))
        for name, value in prepared.headers.items():
            request.setRawHeader(name.encode("ascii"), value.encode("utf-8"))

        payload = QByteArray((prepared.body or "").encode("utf-8"))
        method = prepared.method.upper()

        self._prepared = prepared
        self._started = time.monotonic()

        if method == "GET":
            reply = self._manager.get(request)
        elif method == "POST":
            reply = self._manager.post(request, payload)
        elif method == "PUT":
            reply = self._manager.put(request, payload)
        elif method == "DELETE":
            reply = self._manager.deleteResource(request)
        else:
            reply = self._manager.sendCustomRequest(request, method.encode("ascii"), payload)

        self._reply = reply
        reply.finished.connect(self._finished)

    def _finished(self) -> None:
        """Read the reply, hand it on, and let go of it."""
        reply = self._reply
        prepared = self._prepared
        self._reply = None
        self._prepared = None
        if reply is None or prepared is None:
            return

        duration_ms = int((time.monotonic() - self._started) * 1000)
        body = bytes(reply.readAll().data()).decode("utf-8", "replace")

        status_attribute = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        reason_attribute = reply.attribute(QNetworkRequest.Attribute.HttpReasonPhraseAttribute)

        headers = {
            bytes(name.data()).decode("latin-1"): bytes(value.data()).decode("latin-1")
            for name, value in reply.rawHeaderPairs()
        }

        if status_attribute is None:
            # Nothing arrived: refused, unresolved, timed out. The service's own
            # error text would be more useful, but there isn't one to have.
            status = 0
            reason = reply.errorString()
        else:
            status = int(status_attribute)
            reason = str(reason_attribute or "")

        reply.deleteLater()
        self.completed.emit(
            Completed(
                prepared=prepared,
                status=status,
                reason=reason,
                headers=headers,
                body=body,
                duration_ms=duration_ms,
            )
        )
