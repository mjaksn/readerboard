"""A connection to the sign for the scripts that talk to it directly.

The service has its own transport, with a lock and a suppression cache, and none
of that is wanted by a script that stands in front of the sign and asks it
questions. What such a script does want is a link that survives the Ethernet
adapter dropping the connection, which it does often enough mid-session that a
script without a reconnect loses the run.
"""

from __future__ import annotations

import time

import serial

from readerboard.protocol import constants as c

# A reply opens with a run of nulls and the payload follows a moment behind, so
# stopping at the first byte gets a lone null back from every question. Stopping
# at the first quiet spell is no better: a memory configuration reply read that
# way on 2026-09-10 stopped partway through its second entry, because the rest
# arrived more than 200 ms later. So a reply is read until the EOT that ends it,
# or until the timeout. See "STRING files, measured on the sign" in
# docs/protocol-notes.md.
READ_POLL_SECONDS = 0.05
READ_TIMEOUT_SECONDS = 3.0


class Link:
    """A connection to the sign that reopens itself when the adapter drops it."""

    def __init__(self, url: str, baud: int) -> None:
        """Remember where the sign is. Nothing is opened until the first send."""
        self._url = url
        self._baud = baud
        self._port: serial.Serial | None = None
        self.reconnects = 0

    def send(self, packet: bytes, *, attempts: int = 4) -> bool:
        """Send one transmission, reopening the link as many times as it takes."""
        for attempt in range(attempts):
            try:
                if self._port is None:
                    self._port = serial.serial_for_url(self._url, baudrate=self._baud, timeout=2)
                self._port.write(packet)
                self._port.flush()
                return True
            except Exception as err:
                self.close()
                if attempt == attempts - 1:
                    print("     ! giving up on this write: %s" % err)
                    return False
                self.reconnects += 1
                print("     . adapter dropped the link, reconnecting")
                time.sleep(1.5)
        return False

    def request(self, packet: bytes) -> bytes:
        """Send a read and collect the reply up to its EOT, or whatever came before the timeout."""
        if self._port is not None:
            try:
                self._port.reset_input_buffer()
            except Exception:
                self.close()
        if not self.send(packet) or self._port is None:
            return b""

        reply = bytearray()
        for _ in range(max(1, int(READ_TIMEOUT_SECONDS / READ_POLL_SECONDS))):
            try:
                chunk = self._port.read(self._port.in_waiting)
            except Exception as err:
                print("     ! the link failed while reading: %s" % err)
                self.close()
                break
            reply += chunk
            if c.EOT in reply:
                break
            time.sleep(READ_POLL_SECONDS)
        return bytes(reply)

    def close(self) -> None:
        """Close the link, ignoring a port that will not close cleanly."""
        try:
            if self._port is not None:
                self._port.close()
        except Exception:
            pass
        self._port = None
