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

# A reply is read until the EOT that ends it, or until the timeout. What is
# waiting is drained on every poll rather than read once, because over socket://
# pyserial's in_waiting is 1 whenever anything is waiting, not how much. Reading
# it once took one byte a poll, and cut every reply longer than about fifty bytes
# off at the timeout. See "Reading state back" in docs/protocol-notes.md. Each
# poll takes at most READ_LIMIT_BYTES, so a link that never stops delivering
# still reaches the EOT check and the timeout.
READ_POLL_SECONDS = 0.05
READ_TIMEOUT_SECONDS = 3.0
READ_LIMIT_BYTES = 4096


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
                budget = READ_LIMIT_BYTES
                while budget > 0 and (waiting := self._port.in_waiting):
                    asked = min(waiting, budget)
                    reply += self._port.read(asked)
                    budget -= asked
            except Exception as err:
                print("     ! the link failed while reading: %s" % err)
                self.close()
                break
            # Only an EOT after the reply's STX closes it. One before that is the
            # tail of something the line was already carrying.
            start = reply.find(c.STX)
            if start >= 0 and reply.find(c.EOT, start) >= 0:
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
