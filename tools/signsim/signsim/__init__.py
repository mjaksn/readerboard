"""A stand-in for a BetaBrite Classic, for watching what the service sends it.

The modules divide on one line: ``framing``, ``spans``, ``decode``, ``model``
and ``names`` are pure and import nothing from Qt, so the protocol reading is
testable in CI where PySide6 is not installed. ``server``, ``window`` and
``app`` are the Qt half and hold no protocol knowledge of their own.
"""
