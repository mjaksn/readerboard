"""A desktop client for exercising the readerboard service's HTTP surface.

Everything in this package except :mod:`relayclient.net`,
:mod:`relayclient.dialogs`, :mod:`relayclient.window` and :mod:`relayclient.app`
imports no Qt, so the half that holds the logic can be tested where Qt is not
installed. That split is the same one the sign simulator makes, and for the same
reason.
"""
