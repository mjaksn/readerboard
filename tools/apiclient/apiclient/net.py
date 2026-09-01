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

import json
import time
from dataclasses import dataclass, field

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from apiclient.request import Prepared
from apiclient.skew import DESCRIPTION_PATH

# Qt 6 disables transfer timeouts by default, and this client has exactly one
# in-flight slot: a reply that never finishes would leave Send disabled for the
# rest of the run. Thirty seconds is far longer than any call here should take,
# since the service answers the HTTP request rather than waiting on the sign,
# and long enough not to cut off a slow one.
TRANSFER_TIMEOUT_MS = 30_000


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
        request.setTransferTimeout(TRANSFER_TIMEOUT_MS)
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


class DescriptionFetcher(QObject):
    """Asks a service to describe itself, on a connection of its own.

    Deliberately not routed through :class:`Caller`. This is not a call anybody
    made, so it must not occupy the one in-flight slot, must not turn the Send
    button off, and must not appear in the history beside the calls that were
    actually asked for.
    """

    fetched = Signal(str, object)
    failed = Signal(str, str, bool)
    # An address whose fetch was abandoned, so whoever recorded it as being
    # checked can stop believing that. Separate from `failed` because nothing
    # went wrong and there is nothing to report on screen.
    superseded = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        """Build the fetcher with its own network manager."""
        super().__init__(parent)
        self._manager = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        # Carried through rather than re-read on the way out: by the time this
        # answers, the address on screen may be a different one.
        self._address = ""

    def fetch(self, base_url: str) -> None:
        """Fetch the description from a base URL, replacing any fetch in progress."""
        # Cleared before the abort, not after. Aborting drives `finished`, and
        # with the old reply still in place that would report a cancellation
        # nobody asked about and release an address that was never checked.
        stale, self._reply = self._reply, None
        stale_address = self._address
        if stale is not None:
            stale.abort()
            stale.deleteLater()
            # Aborting means `_finished` returns without emitting, so nothing
            # else would ever say that this address went unchecked.
            if stale_address:
                self.superseded.emit(stale_address)

        self._address = base_url.rstrip("/")
        request = QNetworkRequest(QUrl(self._address + DESCRIPTION_PATH))
        request.setTransferTimeout(TRANSFER_TIMEOUT_MS)
        request.setRawHeader(b"Accept", b"application/json")
        reply = self._manager.get(request)
        self._reply = reply
        reply.finished.connect(self._finished)

    def _finished(self) -> None:
        """Parse what came back, or say why it could not be had."""
        reply = self._reply
        self._reply = None
        if reply is None:
            return
        address = self._address

        body = bytes(reply.readAll().data()).decode("utf-8", "replace")
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.errorString()
        reply.deleteLater()

        if status is None:
            # Nothing arrived. Worth trying again once something does, so this
            # says so: the address may simply not have been up yet.
            self.failed.emit(address, error, True)
            return
        if int(status) != 200:
            # It answered, and has no description to give. Asking again on every
            # later call would be noise, so this one is final.
            self.failed.emit(
                address, "the service answered %d for %s" % (int(status), DESCRIPTION_PATH), False
            )
            return
        try:
            document = json.loads(body)
        except ValueError as err:
            self.failed.emit(address, "the description did not parse: %s" % err, False)
            return
        # Outside the try. A direct connection runs the slot synchronously, so a
        # ValueError raised anywhere downstream would otherwise be caught here
        # and reported as a parse failure it had nothing to do with.
        self.fetched.emit(address, document)
