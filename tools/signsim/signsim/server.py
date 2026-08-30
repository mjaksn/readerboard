"""Stand in for the sign at the end of a TCP socket.

The service reaches a real sign through an Ethernet to RS-232 adapter, which it
opens as ``socket://host:port``. Nothing in that URL says the far end has to be
an adapter, so a listener here is a drop-in destination: point ``serial_url`` at
this and the service talks to the window instead of to the wall. No setting is
added to the service and no code in it changes.

Each connection gets its own scanner, because two clients sharing one would
interleave their bytes into frames neither of them sent. The service opens one
connection and holds it, so in practice there is one, but a second one arriving
is a thing worth showing rather than a thing worth breaking on.

This is the only module here that knows about Qt networking. Everything it
emits is a plain object from the pure core, so the window never touches a socket
and the core never touches an event loop.
"""

from __future__ import annotations

from functools import partial

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket

from signsim.decode import DecodedTransmission, decode
from signsim.framing import FrameScanner


class SignEndpoint(QObject):
    """A TCP listener that decodes everything written to it."""

    transmission = Signal(object)
    """One decoded transmission, as a :class:`DecodedTransmission`."""

    client_connected = Signal(str)
    """A client attached. Carries its address for the status bar."""

    client_disconnected = Signal(str)
    """A client detached. Carries its address."""

    pending_changed = Signal(int)
    """How many buffered bytes are not yet a whole transmission."""

    def __init__(self, parent: QObject | None = None) -> None:
        """Create a listener. Nothing is bound until :meth:`listen`."""
        super().__init__(parent)
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._accept)
        self._scanners: dict[QTcpSocket, FrameScanner] = {}
        self._paused = False

    # == the listener ======================================================

    def listen(self, host: str, port: int) -> None:
        """Bind and start accepting, or raise :class:`OSError` saying why not."""
        address = QHostAddress(host)
        if address.isNull():
            raise OSError("%r is not an address this can bind to" % host)
        if not self._server.listen(address, port):
            raise OSError(
                "could not listen on %s:%d: %s"
                % (host, port, self._server.errorString())
            )

    @property
    def endpoint(self) -> str:
        """Where this is listening, in the form a pyserial URL wants."""
        if not self._server.isListening():
            return "not listening"
        return "%s:%d" % (
            self._server.serverAddress().toString(),
            self._server.serverPort(),
        )

    @property
    def serial_url(self) -> str:
        """The value to put in the service's ``serial_url`` setting."""
        return "socket://%s" % self.endpoint

    @property
    def client_count(self) -> int:
        """How many clients are attached."""
        return len(self._scanners)

    def set_paused(self, paused: bool) -> None:
        """Stop or resume decoding.

        Paused means the bytes are still read off the socket and thrown away.
        Leaving them unread would fill the kernel buffer and eventually stall
        the service, which is a worse thing to do to somebody who only wanted
        the log to stop scrolling.
        """
        self._paused = paused

    def close(self) -> None:
        """Stop listening and drop every client."""
        for socket in list(self._scanners):
            socket.disconnectFromHost()
        self._scanners.clear()
        self._server.close()

    # == connections =======================================================

    def _accept(self) -> None:
        """Take a new client and give it a scanner of its own."""
        # Guarded by hasPendingConnections rather than by a None check on the
        # result, because the binding types the result as non-optional and a
        # check against None is then unreachable code.
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._scanners[socket] = FrameScanner()
            socket.readyRead.connect(partial(self._read, socket))
            socket.disconnected.connect(partial(self._drop, socket))
            self.client_connected.emit(_describe(socket))

    def _drop(self, socket: QTcpSocket) -> None:
        """Forget a client that went away."""
        self._scanners.pop(socket, None)
        self.client_disconnected.emit(_describe(socket))
        socket.deleteLater()

    def _read(self, socket: QTcpSocket) -> None:
        """Feed whatever arrived into that client's scanner."""
        data = bytes(socket.readAll().data())
        scanner = self._scanners.get(socket)
        if scanner is None or self._paused:
            return

        for found in scanner.feed(data):
            self.transmission.emit(decode(found))
        self.pending_changed.emit(scanner.pending_bytes)


def _describe(socket: QTcpSocket) -> str:
    """Name a client the way a status bar should show it."""
    return "%s:%d" % (socket.peerAddress().toString(), socket.peerPort())


__all__ = ["DecodedTransmission", "SignEndpoint"]
