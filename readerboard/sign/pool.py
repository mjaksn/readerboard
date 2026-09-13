"""Whether the configured files fit the memory pool the sign actually has.

The pool is small. A BetaBrite Classic reported 5482 bytes on 2026-09-12, a
little over twice what the default configuration takes. What the sign does with
a configuration bigger than that has not been measured, and the document says
only that the sum "should not exceed" the pool, so nothing here relies on the
sign refusing one. The question is settled on this side of the wire instead,
before the write that would apply it, which erases every message on the sign.

The check is in two tiers, and the division is not arbitrary.

``Settings`` is the first tier and cannot ask the sign. It is validated wherever
settings are read, which includes the tests, ``scripts/dump_openapi.py`` and any
machine whose sign is unplugged, so a validator that opened a serial port would
turn a configuration error into a connection error and would fail on machines
that have no sign to fail about. It measures against
``config.ASSUMED_SIGN_MEMORY_POOL``, the figure this hardware reported.

This module is the second tier. It runs at startup with the link already open,
asks the sign what it actually has, and uses the sign's own answer in preference
to the assumption. Two things follow from that:

- A sign that says nothing, says something unreadable, or is a ``loop://`` URL
  echoing the question back is not a reason to refuse to start. The service is
  expected to survive a Pi losing power, and a service that will not boot needs
  somebody to log in, which is precisely what it exists to avoid. The
  assumption stands in, at WARNING, and startup carries on exactly as it did
  before this check existed.
- A sign that answers clearly and does not have room is a different thing
  entirely. Nothing is wrong with the link, the configuration is simply too big
  for the hardware, and that needs a person whichever way it is handled. So it
  stops the service rather than erasing the sign for a pool that cannot work.

The caller is expected to ask only when a reconfiguration is actually due. That
keeps an ordinary restart free of both the read and its cost on the display, and
it means nothing here can cause an erase that would not have happened anyway.
See ``readerboard.api.app``.
"""

from __future__ import annotations

import logging

from readerboard.protocol import frames
from readerboard.protocol.replies import ReplyError, parse_general_information
from readerboard.sign.controller import SignController
from readerboard.sign.layout import Layout
from readerboard.transport.base import TransportError

logger = logging.getLogger(__name__)


class PoolTooLarge(RuntimeError):
    """The configured files need more memory than the sign has."""


async def measure(controller: SignController, *, fallback: int) -> int:
    """Ask the sign how big its memory pool is, falling back when it will not say.

    The total is what is wanted here rather than the free size. A memory
    configuration hands the whole pool back before it takes any of it, so what
    is free before the write says nothing about what the write may claim.

    A pool of zero is treated as no answer. It is not a size any sign has, so it
    is a reply that parsed rather than a sign that is full, and refusing to start
    over it would be refusing to start over line noise.
    """
    try:
        reply = await controller.read_special(frames.read_general_information())
        information = parse_general_information(reply)
    except (TransportError, ReplyError) as err:
        logger.warning(
            "could not read the sign's memory pool (%s); assuming the %d bytes a "
            "BetaBrite Classic has. If this sign is smaller than that, a pool too big "
            "for it will be written and nothing here will have caught it.",
            err,
            fallback,
        )
        return fallback
    except Exception:
        # The two above are the failures this knows how to have. Anything else
        # is a bug in the asking, and a bug in the asking must not be able to
        # stop the service starting: that has happened here once already, when
        # an alert the priority file could no longer hold raised past the
        # handler in the lifespan and the service then failed to start on every
        # attempt, fixable only by editing a state file by hand on the machine.
        # So it is logged with its traceback and the assumption stands in.
        logger.exception(
            "reading the sign's memory pool failed in a way this did not plan for; "
            "assuming the %d bytes a BetaBrite Classic has",
            fallback,
        )
        return fallback

    if information.memory_total <= 0:
        logger.warning(
            "the sign reported a memory pool of %d bytes, which is not a size any sign "
            "has; assuming the %d bytes a BetaBrite Classic has. The whole reply was %r.",
            information.memory_total,
            fallback,
            information.raw,
        )
        return fallback

    logger.info(
        "the sign reports a memory pool of %d bytes, %d of them free",
        information.memory_total,
        information.memory_free,
    )
    return information.memory_total


def check_fits(layout: Layout, pool: int) -> None:
    """Raise :class:`PoolTooLarge` if this layout cannot fit in ``pool`` bytes.

    The message names all three things somebody would otherwise have to work
    out: what was configured, what it needs, and what the sign has.
    """
    claimed = frames.memory_claimed(layout.allocations())
    if claimed <= pool:
        return

    raise PoolTooLarge(
        "slot_count %d at slot_capacity %d and variable_count %d at variable_capacity "
        "%d need %d bytes of the sign's memory pool, and the sign has %d. Lower one of "
        "them and start the service again. Nothing has been written, so the sign still "
        "holds whatever it held."
        % (
            layout.slot_count,
            layout.slot_capacity,
            layout.variable_count,
            layout.variable_capacity,
            claimed,
            pool,
        )
    )
