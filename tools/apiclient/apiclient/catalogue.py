"""Every operation the service offers, written down as data.

The window builds its forms from this table rather than from fifteen hand-written
panels, which is what makes "the client can call any endpoint" true by
construction. Adding a route to the service is then a row here rather than a new
screen.

The table is hand-written on purpose. Generating the forms from
``docs/openapi.json`` at run time would put more logic in this tool, not less,
and it would lose the thing the description cannot carry: which enumeration a
field draws from, and which formatter reads the response back. Hand-written
tables drift, so ``tests/test_catalogue.py`` diffs this one against the checked-in
OpenAPI description in both directions. A route added to the service fails this
tool's tests in the same commit.
"""

from __future__ import annotations

from dataclasses import dataclass

# The enumeration sets a field can draw from. These are the client's own names
# for them rather than the service's paths, so that a field says which
# vocabulary it draws from without naming the endpoint that happens to serve it.
MARKUP_TOKENS = "markup-tokens"
DISPLAY_MODES = "display-modes"
TEXT_POSITIONS = "text-positions"
CONTROL_COMMANDS = "control-commands"

SET_TITLES = {
    MARKUP_TOKENS: "Markup tokens",
    DISPLAY_MODES: "Display modes",
    TEXT_POSITIONS: "Text positions",
    CONTROL_COMMANDS: "Control commands",
}

# The order the enumeration panel lists them in.
SET_ORDER = (MARKUP_TOKENS, DISPLAY_MODES, TEXT_POSITIONS, CONTROL_COMMANDS)


@dataclass(frozen=True, slots=True)
class Input:
    """One value the caller supplies, in the path or in the body.

    ``prefill`` is what the field starts out holding, which is not the same as
    the schema's default and is deliberately named so it cannot be mistaken for
    it. Usually the two agree, and ``tests/test_catalogue.py`` insists on that
    wherever the schema declares a default. Where it declares none the prefill
    is a convenience of this client's own, offered without the service having
    promised it.
    """

    name: str
    kind: str = "text"
    required: bool = False
    prefill: object = None
    enum_set: str | None = None
    markup: bool = False
    slot_keys: bool = False
    description: str = ""

    @property
    def label(self) -> str:
        """Return the field name as a person would rather read it."""
        return self.name.replace("_", " ")


@dataclass(frozen=True, slots=True)
class Operation:
    """One callable endpoint."""

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
    note: str = ""

    @property
    def signature(self) -> str:
        """Return the method and path as they read in a routing table."""
        return "%s %s" % (self.method, self.path)


_MESSAGE_KEY = Input(
    name="key",
    required=True,
    slot_keys=True,
    description="the name of the slot, chosen by whoever owns it",
)

_DISPLAY_MODE = Input(
    name="display_mode",
    prefill="HOLD",
    enum_set=DISPLAY_MODES,
    description="how the sign presents the message",
)

_POSITION = Input(
    name="position",
    prefill="MIDDLE",
    enum_set=TEXT_POSITIONS,
    description="where the text sits vertically",
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
                markup=True,
                description="the message, including markup tokens such as <red> and <degree>",
            ),
            _DISPLAY_MODE,
            _POSITION,
            Input(
                name="order",
                kind="int",
                prefill=0,
                description="lower numbers play earlier in the rotation",
            ),
            Input(
                name="ttl_seconds",
                kind="float",
                description="drop the message this many seconds from now; leave empty to keep it",
            ),
            Input(
                name="source",
                description="who registered this, recorded so the slot list is readable",
            ),
        ),
        formatter="slot",
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
                markup=True,
                description="the alert text; the priority file holds 125 rendered bytes",
            ),
            _DISPLAY_MODE,
            _POSITION,
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
        id="display_modes",
        group="Enumerations",
        method="GET",
        path="/enumerations/display-modes",
        summary="Ways the sign can present a message",
        formatter="tokens",
        loads=DISPLAY_MODES,
    ),
    Operation(
        id="text_positions",
        group="Enumerations",
        method="GET",
        path="/enumerations/text-positions",
        summary="Where text sits vertically",
        formatter="tokens",
        loads=TEXT_POSITIONS,
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
GROUP_ORDER = ("Health", "Messages", "Alerts", "Sign", "Enumerations")

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


