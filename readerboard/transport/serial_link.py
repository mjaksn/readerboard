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
        self._retry_delay = backoff_initial
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
        """Send one complete transmission over a link that is already open.

        The link is not opened here. When it is down this fails at once, rather
        than blocking the caller for the length of a connection attempt, which
        against a network sign at a wrong or dead address is the operating
        system's whole connect timeout. Opening the link is the reconnect loop's
        job, and the first open is startup's.

        The ``is_open`` check is before the lock on purpose. The reconnect loop
        holds that lock for the length of a connect, so a write that waited for
        the lock would wait out exactly the block this exists to avoid. Reading
        ``is_open`` outside the lock is safe: if the link drops between the check
        and the write, the write fails and is reported like any other failure.
        """
        if not self.is_open:
            raise self._down_error()
        with self._lock:
            port = self._port
            if port is None:
                # Dropped between the check and the lock. The same answer, and
                # it has to carry the same detail: this reaches an API caller as
                # the body of a 503, where "is down" on its own says neither why
                # nor for how long.
                raise self._down_error()
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

        if self._retry_after > self._monotonic():
            raise self._down_error()

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
        self._retry_delay = self._backoff_initial
        self._last_error = None

    def _down_error(self) -> TransportError:
        """Describe a link that is down, in the one way every path reporting it uses.

        Three paths report it, and the text is the whole of what a caller gets:
        the service maps :class:`TransportError` to a 503 and uses this as the
        body. So the reason the link failed and the wait until the next attempt
        belong in all three, not just the ones where they were convenient.
        """
        waiting = max(0.0, self._retry_after - self._monotonic())
        return TransportError(
            "link to %s is down (%s); next attempt in %.1fs"
            % (self._url, self._last_error or "reason unknown", waiting)
        )

    def _record_failure(self, err: Exception) -> None:
        # The delay is carried forward and doubled rather than recomputed as
        # ``initial * 2 ** (failures - 1)``, because that exponent grows without
        # bound while the count does. A sign switched off over a weekend reaches
        # attempt 1025 in about seventeen hours at the sixty second cap, and
        # ``1.0 * 2 ** 1024`` raises OverflowError rather than returning
        # infinity: the base is a float, so the result cannot be represented.
        # That escaped as an ArithmeticError, which nothing on the way out
        # caught, and killed the one task able to reopen the link.
        self._failures += 1
        self._last_error = str(err)
        delay = min(self._backoff_max, self._retry_delay)
        self._retry_delay = min(self._backoff_max, self._retry_delay * 2)
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
