"""The real link to the sign, over a serial port or over the network.

``serial.serial_for_url`` accepts both ``socket://192.168.2.51:4001`` for an
Ethernet to RS-232 adapter and ``/dev/ttyUSB0`` for a cable plugged straight
in, so one configuration value covers both ways of reaching the sign. It also
accepts ``loop://``, which is what the tests use to exercise this module rather
than a stand-in for it.

The link is opened once and held, not opened and closed around every write. The
old implementation did the latter, and paid for it with a two second sleep on
every request and a race whenever two callers arrived together.

Reconnection is deliberately passive. When the link is down, ``ensure_open``
refuses to try again until a backoff window has passed, and ``write`` fails
immediately rather than blocking a request behind a device that is not there.
Something has to drive the retries; that is the controller's reconnect loop.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import serial

from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)


class SerialTransport:
    """A held-open pyserial link with capped exponential backoff on failure."""

    def __init__(
        self,
        url: str,
        *,
        baud_rate: int = 9600,
        timeout: float = 10.0,
        backoff_initial: float = 1.0,
        backoff_max: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the link. Nothing is opened until :meth:`ensure_open`."""
        self._url = url
        self._baud_rate = baud_rate
        self._timeout = timeout
        self._backoff_initial = backoff_initial
        self._backoff_max = backoff_max
        self._monotonic = monotonic

        self._port: serial.Serial | None = None
        self._lock = threading.Lock()
        self._failures = 0
        self._retry_after = 0.0
        self._last_error: str | None = None

    @property
    def is_open(self) -> bool:
        """Whether the link is currently up."""
        port = self._port
        return port is not None and bool(port.is_open)

    @property
    def description(self) -> str:
        """A short name for the link, safe to log."""
        return self._url

    @property
    def last_error(self) -> str | None:
        """Why the link last failed, or None if it has not failed since it opened."""
        return self._last_error

    def seconds_until_retry(self) -> float:
        """How long until :meth:`ensure_open` will try again. Zero means now."""
        if self.is_open:
            return 0.0
        return max(0.0, self._retry_after - self._monotonic())

    def ensure_open(self) -> None:
        """Open the link if it is down, honouring the backoff window."""
        with self._lock:
            self._ensure_open_locked()

    def write(self, data: bytes) -> None:
        """Send one complete transmission, opening the link first if needed."""
        with self._lock:
            self._ensure_open_locked()
            port = self._port
            assert port is not None  # _ensure_open_locked guarantees this
            try:
                port.write(data)
                port.flush()
            except (serial.SerialException, OSError) as err:
                self._record_failure(err)
                self._close_locked()
                raise TransportError("write to %s failed: %s" % (self._url, err)) from err

    def close(self) -> None:
        """Close the link. Closing an already closed link does nothing."""
        with self._lock:
            self._close_locked()

    # == internals ==========================================================

    def _ensure_open_locked(self) -> None:
        if self.is_open:
            return

        waiting = max(0.0, self._retry_after - self._monotonic())
        if waiting > 0:
            raise TransportError(
                "link to %s is down (%s); next attempt in %.1fs"
                % (self._url, self._last_error or "reason unknown", waiting)
            )

        try:
            self._port = serial.serial_for_url(
                self._url, baudrate=self._baud_rate, timeout=self._timeout
            )
        except (serial.SerialException, OSError, ValueError) as err:
            self._record_failure(err)
            self._port = None
            raise TransportError("could not open %s: %s" % (self._url, err)) from err

        if self._failures:
            logger.info("link to %s is back after %d failed attempts", self._url, self._failures)
        else:
            logger.info("link to %s is open", self._url)
        self._failures = 0
        self._retry_after = 0.0
        self._last_error = None

    def _record_failure(self, err: Exception) -> None:
        self._failures += 1
        self._last_error = str(err)
        delay = min(self._backoff_max, self._backoff_initial * (2 ** (self._failures - 1)))
        self._retry_after = self._monotonic() + delay
        logger.warning(
            "link to %s failed (attempt %d): %s; backing off %.1fs",
            self._url,
            self._failures,
            err,
            delay,
        )

    def _close_locked(self) -> None:
        port = self._port
        self._port = None
        if port is None:
            return
        try:
            port.close()
        except Exception:
            # A port that will not close cleanly is still a port we are done
            # with, and there is nothing useful for a caller to do about it.
            logger.debug("ignoring error while closing %s", self._url, exc_info=True)
