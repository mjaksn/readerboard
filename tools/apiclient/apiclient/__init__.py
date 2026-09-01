"""A desktop client for exercising the readerboard service's HTTP surface.

Everything in this package except :mod:`apiclient.net`,
:mod:`apiclient.dialogs`, :mod:`apiclient.window` and :mod:`apiclient.app`
imports no Qt, so the half that holds the logic can be tested where Qt is not
installed. That split is the same one the sign simulator makes, and for the same
reason.
"""
