"""Every operation the service offers, written down as data.

The window builds its forms from this table rather than from twenty-two hand-written
panels, which is what makes "the client can call any endpoint" true by
construction. Adding a route to the service is then a row here rather than a new
screen.

The table is hand-written on purpose. Generating the forms from
``docs/openapi.json`` at run time would put more logic in this tool, not less,
and it would lose the thing the description cannot carry: which enumeration a
field draws from, and which formatter reads the response back. Hand-written
tables drift, so ``tools/apiclient/tests/test_catalogue.py`` diffs this one
against the checked-in OpenAPI description in both directions. A route added to
the service fails this tool's tests in the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass

# The enumeration sets a field can draw from. These are the client's own names
# for them rather than the service's paths, so that a field says which
# vocabulary it draws from without naming the endpoint that happens to serve it.
MARKUP_TOKENS = "markup-tokens"
VALUE_TOKENS = "value-tokens"
DISPLAY_MODES = "display-modes"
CONTROL_COMMANDS = "control-commands"

SET_TITLES = {
    MARKUP_TOKENS: "Markup tokens",
    VALUE_TOKENS: "Value tokens",
    DISPLAY_MODES: "Display modes",
    CONTROL_COMMANDS: "Control commands",
}

# The order the enumeration panel lists them in.
SET_ORDER = (MARKUP_TOKENS, VALUE_TOKENS, DISPLAY_MODES, CONTROL_COMMANDS)


@dataclass(frozen=True, slots=True)
class Input:
    """One value the caller supplies, in the path or in the body.

    ``markup`` names the token set a text field offers through its Insert
    token button: a message takes the markup tokens, and a variable's value
    takes the value tokens, which are the same less the ones the sign cannot
    draw from inside a variable.

    ``keys_from`` names the operation listing what a path parameter can name,
    and puts a Load button beside the box that fills it from that list. Each
    item in the list carries the key under the same name the parameter has,
    ``key`` for a message and ``name`` for a variable, so the parameter's own
    name is what is read out of each.

    ``prefill`` is what the field starts out holding, which is not the same as
    the schema's default and is deliberately named so it cannot be mistaken for
    it. Every prefill has a declared default behind it, and
    ``test_no_prefill_is_offered_that_the_schema_does_not_back`` in
    ``tools/apiclient/tests/test_catalogue.py`` refuses one that does not, so
    the client promises the caller nothing the service has not.

    ``fill_from`` names an operation that reads the same resource this one
    writes, and does two things at once: it puts a button under this field's
    label, and it says what that button calls. Pressing it fills every body
    field the response has a value for, not only this one; the field owns the
    button because that is where it is worth having, next to the content
    somebody is about to edit.

    ``seconds_until`` names the field in that response which this one is a
    duration to. The service takes a deadline as seconds from now and reports
    it as the moment it falls, so the two are the same fact in different
    units and neither side can use the other's directly.
    """

    name: str
    kind: str = "text"
    required: bool = False
    prefill: object = None
    enum_set: str | None = None
    markup: str | None = None
    keys_from: str | None = None
    fill_from: str | None = None
    seconds_until: str | None = None
    description: str = ""

    @property
    def label(self) -> str:
        """Return the field name as a person would rather read it."""
        return self.name.replace("_", " ")


@dataclass(frozen=True, slots=True)
class Operation:
    """One callable endpoint.

    ``destructive`` and ``confirm`` both gate a send behind a yes/no prompt, and
    they say different things. ``destructive`` marks an operation that throws
    work away, such as clearing every message, and asks a plain question before
    it. ``confirm`` carries the text for a warning-coloured prompt in front of an
    operation that is not about losing data but is disruptive to run, such as
    rebooting the sign. An operation sets one or the other, not both.
    """

    id: str
    group: str
    method: str
    path: str
    summary: str
    needs_key: bool = False
    path_inputs: tuple[Input, ...] = ()
    body: tuple[Input, ...] = ()
    formatter: str = "generic"
    loads: str | None = None
    destructive: bool = False
    confirm: str = ""
    note: str = ""

    @property
    def signature(self) -> str:
        """Return the method and path as they read in a routing table."""
        return "%s %s" % (self.method, self.path)


_MESSAGE_KEY = Input(
    name="key",
    required=True,
    keys_from="list_messages",
    description="the name of the slot, chosen by whoever owns it",
)

_VARIABLE_NAME = Input(
    name="name",
    required=True,
    keys_from="list_variables",
    description="lowercase letters, digits and underscores; a message calls it as <var:name>",
)

_DISPLAY_MODE = Input(
    name="display_mode",
    prefill="HOLD",
    enum_set=DISPLAY_MODES,
    description="how the sign presents the message",
)

OPERATIONS: tuple[Operation, ...] = (
    # == Messages =========================================================
    Operation(
        id="list_messages",
        group="Messages",
        method="GET",
        path="/messages",
        summary="List the messages sharing the sign",
        formatter="slots",
    ),
    Operation(
        id="get_message",
        group="Messages",
        method="GET",
        path="/messages/{key}",
        summary="Read one message",
        path_inputs=(_MESSAGE_KEY,),
        formatter="slot",
    ),
    Operation(
        id="put_message",
        group="Messages",
        method="PUT",
        path="/messages/{key}",
        summary="Register or replace a message",
        needs_key=True,
        path_inputs=(_MESSAGE_KEY,),
        body=(
            Input(
                name="message",
                kind="textarea",
                required=True,
                markup=MARKUP_TOKENS,
                fill_from="get_message",
                description=(
                    "the message, including markup tokens such as <red> and <degree>, "
                    "and <var:name> to call a variable"
                ),
            ),
            _DISPLAY_MODE,
            Input(
                name="order",
                kind="int",
                prefill=0,
                description="lower numbers play earlier in the rotation",
            ),
            Input(
                name="ttl_seconds",
                kind="float",
                seconds_until="expires_at",
                description="act on the message this many seconds from now; empty keeps it",
            ),
            Input(
                name="delete_on_expiry",
                kind="bool",
                prefill=True,
                description="true gives the slot back; false keeps it and hides the message",
            ),
            Input(
                name="source",
                description="who registered this, recorded so the slot list is readable",
            ),
        ),
        formatter="slot",
    ),
    Operation(
        id="set_message_active",
        group="Messages",
        method="PUT",
        path="/messages/{key}/active",
        summary="Show or hide a message without unregistering it",
        needs_key=True,
        path_inputs=(_MESSAGE_KEY,),
        body=(
            Input(
                name="active",
                kind="bool",
                required=True,
                description="true puts it back into the rotation, false takes it off the display",
            ),
        ),
        formatter="slot",
        note=(
            "A hidden message keeps its slot, its file, its text and its place in the "
            "order, so showing it again needs no copy of what it said. The messages "
            "still showing carry on without a blank or a restart."
        ),
    ),
    Operation(
        id="delete_message",
        group="Messages",
        method="DELETE",
        path="/messages/{key}",
        summary="Take a message off the sign",
        needs_key=True,
        path_inputs=(_MESSAGE_KEY,),
        formatter="empty",
    ),
    Operation(
        id="clear_messages",
        group="Messages",
        method="DELETE",
        path="/messages",
        summary="Take every message off the sign",
        needs_key=True,
        formatter="empty",
        destructive=True,
        note="This clears every slot at once, leaving the sign showing nothing.",
    ),
    # == Variables ========================================================
    Operation(
        id="list_variables",
        group="Variables",
        method="GET",
        path="/variables",
        summary="List the variables messages can call",
        formatter="variables",
    ),
    Operation(
        id="get_variable",
        group="Variables",
        method="GET",
        path="/variables/{name}",
        summary="Read one variable",
        path_inputs=(_VARIABLE_NAME,),
        formatter="variable",
    ),
    Operation(
        id="put_variable",
        group="Variables",
        method="PUT",
        path="/variables/{name}",
        summary="Create or change a variable",
        needs_key=True,
        path_inputs=(_VARIABLE_NAME,),
        body=(
            Input(
                name="value",
                kind="textarea",
                required=True,
                markup=VALUE_TOKENS,
                fill_from="get_variable",
                description=(
                    "the value; its formatting carries on into the message after the call"
                ),
            ),
            Input(
                name="ttl_seconds",
                kind="float",
                seconds_until="expires_at",
                description="show the stale value this many seconds from now; empty keeps it",
            ),
            Input(
                name="stale_value",
                prefill="",
                description="what to show once the ttl passes, such as --; empty shows nothing",
            ),
            Input(
                name="source",
                description="who wrote this, recorded so the variable list is readable",
            ),
        ),
        formatter="variable",
        note=(
            "Changing a value does not blank the sign or restart the message calling it; "
            "a scrolling message picks it up on its next pass."
        ),
    ),
    Operation(
        id="delete_variable",
        group="Variables",
        method="DELETE",
        path="/variables/{name}",
        summary="Delete a variable",
        needs_key=True,
        path_inputs=(_VARIABLE_NAME,),
        formatter="empty",
        note="Refused while any message or the alert still calls it.",
    ),
    # == Alerts ===========================================================
    Operation(
        id="get_alert",
        group="Alerts",
        method="GET",
        path="/alerts",
        summary="Read the alert holding the sign",
        formatter="alert",
    ),
    Operation(
        id="post_alert",
        group="Alerts",
        method="POST",
        path="/alerts",
        summary="Take the sign over with an alert",
        needs_key=True,
        body=(
            Input(
                name="message",
                kind="textarea",
                required=True,
                markup=MARKUP_TOKENS,
                description="the alert text; the priority file holds 125 rendered bytes",
            ),
            _DISPLAY_MODE,
            Input(
                name="ttl_seconds",
                kind="float",
                description="release the sign this many seconds from now; empty means hold it",
            ),
        ),
        formatter="alert",
        note="An alert suppresses every other message until it is released.",
    ),
    Operation(
        id="delete_alert",
        group="Alerts",
        method="DELETE",
        path="/alerts",
        summary="Give the sign back",
        needs_key=True,
        formatter="empty",
    ),
    # == The sign itself ==================================================
    Operation(
        id="sign_information",
        group="Sign",
        method="GET",
        path="/sign/information",
        summary="Ask the sign what it is and how it is doing",
        needs_key=True,
        formatter="sign_information",
    ),
    Operation(
        id="sync_clock",
        group="Sign",
        method="POST",
        path="/sign/sync-clock",
        summary="Set the sign's clock now",
        needs_key=True,
        formatter="clock",
    ),
    Operation(
        id="send_command",
        group="Sign",
        method="POST",
        path="/sign/command",
        summary="Send a control command to the sign",
        needs_key=True,
        body=(
            Input(
                name="command",
                required=True,
                enum_set=CONTROL_COMMANDS,
                description="one of the sign's own control commands",
            ),
            Input(
                name="parameter",
                prefill="",
                description="the command's parameter, if it takes one",
            ),
        ),
        formatter="empty",
    ),
    Operation(
        id="reboot_sign",
        group="Sign",
        method="POST",
        path="/sign/reboot",
        summary="Reboot the sign to recover it",
        needs_key=True,
        formatter="empty",
        confirm=(
            "Rebooting resets the sign and blanks it for about ten seconds "
            "before the messages come back. Use it only to recover a sign that "
            "has stopped responding, not to clear messages. Send it?"
        ),
        note=(
            "A recovery tool. It resets a wedged sign and restores the display "
            "from the service's record; the sign goes blank for about ten "
            "seconds first."
        ),
    ),
    # == Enumerations =====================================================
    Operation(
        id="markup_tokens",
        group="Enumerations",
        method="GET",
        path="/enumerations/markup-tokens",
        summary="Markup tokens a message may contain",
        formatter="tokens",
        loads=MARKUP_TOKENS,
    ),
    Operation(
        id="value_tokens",
        group="Enumerations",
        method="GET",
        path="/enumerations/value-tokens",
        summary="Markup tokens a variable's value may contain",
        formatter="tokens",
        loads=VALUE_TOKENS,
    ),
    Operation(
        id="display_modes",
        group="Enumerations",
        method="GET",
        path="/enumerations/display-modes",
        summary="Ways the sign can present a message",
        formatter="tokens",
        loads=DISPLAY_MODES,
    ),
    Operation(
        id="control_commands",
        group="Enumerations",
        method="GET",
        path="/enumerations/control-commands",
        summary="Commands aimed at the sign itself",
        formatter="tokens",
        loads=CONTROL_COMMANDS,
    ),
    # == Health ===========================================================
    Operation(
        id="health",
        group="Health",
        method="GET",
        path="/health",
        summary="Is the service talking to the sign",
        formatter="health",
        note="Needs no API key. Neither does any read; only the writes carry one.",
    ),
)

# The order the operation list shows the groups in. Health first because it is
# what you press to find out whether anything else is worth trying.
GROUP_ORDER = ("Health", "Messages", "Variables", "Alerts", "Sign", "Enumerations")

BY_ID: dict[str, Operation] = {operation.id: operation for operation in OPERATIONS}


def grouped() -> list[tuple[str, list[Operation]]]:
    """Return the operations bucketed by group, in the order the window lists them."""
    buckets: dict[str, list[Operation]] = {name: [] for name in GROUP_ORDER}
    for operation in OPERATIONS:
        buckets[operation.group].append(operation)
    return [(name, buckets[name]) for name in GROUP_ORDER if buckets[name]]


def loaders_for(set_key: str) -> list[Operation]:
    """Return every operation that can fill the given enumeration set."""
    return [op for op in OPERATIONS if op.loads == set_key]


