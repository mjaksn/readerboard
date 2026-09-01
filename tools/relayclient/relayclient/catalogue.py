"""Every operation the service offers, written down as data.

The window builds its forms from this table rather than from twenty hand-written
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
# for them, not the service's paths, because two different endpoints can fill
# the same set: /v2/enumerations/display-modes and /Enumerations/DisplayModes
# say the same thing in different shapes.
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
    is a convenience of this client's own: ``POST /Write/Message`` requires a
    display mode with no default, and starting that field on ``HOLD`` saves
    typing the commonest answer without the service having promised it.
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
class Loads:
    """What an enumeration endpoint fills, and the field its entries are named by.

    The two families spell the name differently. ``/v2`` answers ``name`` for
    every set; the simple endpoints answer ``token_text``, ``display_mode`` or
    ``control_command`` depending on which one was asked. Both normalise to the
    same store on the way in.
    """

    set_key: str
    name_field: str


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
    loads: Loads | None = None
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
        path="/v2/messages",
        summary="List the messages sharing the sign",
        formatter="slots",
    ),
    Operation(
        id="get_message",
        group="Messages",
        method="GET",
        path="/v2/messages/{key}",
        summary="Read one message",
        path_inputs=(_MESSAGE_KEY,),
        formatter="slot",
    ),
    Operation(
        id="put_message",
        group="Messages",
        method="PUT",
        path="/v2/messages/{key}",
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
        path="/v2/messages/{key}",
        summary="Take a message off the sign",
        needs_key=True,
        path_inputs=(_MESSAGE_KEY,),
        formatter="empty",
    ),
    Operation(
        id="clear_messages",
        group="Messages",
        method="DELETE",
        path="/v2/messages",
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
        path="/v2/alerts",
        summary="Read the alert holding the sign",
        formatter="alert",
    ),
    Operation(
        id="post_alert",
        group="Alerts",
        method="POST",
        path="/v2/alerts",
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
        path="/v2/alerts",
        summary="Give the sign back",
        needs_key=True,
        formatter="empty",
    ),
    # == The sign itself ==================================================
    Operation(
        id="sync_clock",
        group="Sign",
        method="POST",
        path="/v2/sign/sync-clock",
        summary="Set the sign's clock now",
        needs_key=True,
        formatter="clock",
    ),
    Operation(
        id="send_command",
        group="Sign",
        method="POST",
        path="/v2/sign/command",
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
    # == Enumerations, the current shape ==================================
    Operation(
        id="v2_markup_tokens",
        group="Enumerations",
        method="GET",
        path="/v2/enumerations/markup-tokens",
        summary="Markup tokens a message may contain",
        formatter="tokens",
        loads=Loads(MARKUP_TOKENS, "name"),
    ),
    Operation(
        id="v2_display_modes",
        group="Enumerations",
        method="GET",
        path="/v2/enumerations/display-modes",
        summary="Ways the sign can present a message",
        formatter="tokens",
        loads=Loads(DISPLAY_MODES, "name"),
    ),
    Operation(
        id="v2_text_positions",
        group="Enumerations",
        method="GET",
        path="/v2/enumerations/text-positions",
        summary="Where text sits vertically",
        formatter="tokens",
        loads=Loads(TEXT_POSITIONS, "name"),
    ),
    Operation(
        id="v2_control_commands",
        group="Enumerations",
        method="GET",
        path="/v2/enumerations/control-commands",
        summary="Commands aimed at the sign itself",
        formatter="tokens",
        loads=Loads(CONTROL_COMMANDS, "name"),
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
    # == The simple surface ===============================================
    Operation(
        id="simple_write_message",
        group="Simple",
        method="POST",
        path="/Write/Message",
        summary="Write a message to the sign",
        needs_key=True,
        body=(
            Input(
                name="display_mode",
                required=True,
                prefill="HOLD",
                enum_set=DISPLAY_MODES,
                description="the display mode to use when showing the message",
            ),
            Input(
                name="message",
                kind="textarea",
                required=True,
                markup=True,
                description="the message, including markup tokens, to display on the sign",
            ),
        ),
        formatter="simple",
        note="Answers 200 whatever happens; the outcome is in the body.",
    ),
    Operation(
        id="simple_control_command",
        group="Simple",
        method="POST",
        path="/Write/ControlCommand",
        summary="Send a control command to the sign",
        needs_key=True,
        body=(
            Input(
                name="command",
                required=True,
                enum_set=CONTROL_COMMANDS,
                description="the control command to send to the sign",
            ),
            Input(name="parameter", prefill="", description="a parameter for the command"),
        ),
        formatter="simple",
        note="Answers 200 whatever happens; the outcome is in the body.",
    ),
    Operation(
        id="simple_display_modes",
        group="Simple",
        method="GET",
        path="/Enumerations/DisplayModes",
        summary="Available display modes",
        formatter="tokens",
        loads=Loads(DISPLAY_MODES, "display_mode"),
    ),
    Operation(
        id="simple_control_commands",
        group="Simple",
        method="GET",
        path="/Enumerations/ControlCommands",
        summary="Available control commands",
        formatter="tokens",
        loads=Loads(CONTROL_COMMANDS, "control_command"),
    ),
    Operation(
        id="simple_markup_tokens",
        group="Simple",
        method="GET",
        path="/Enumerations/MarkupTokens",
        summary="Available markup tokens",
        formatter="tokens",
        loads=Loads(MARKUP_TOKENS, "token_text"),
    ),
)

# The order the operation list shows the groups in. Health first because it is
# what you press to find out whether anything else is worth trying.
GROUP_ORDER = ("Health", "Messages", "Alerts", "Sign", "Enumerations", "Simple")

BY_ID: dict[str, Operation] = {operation.id: operation for operation in OPERATIONS}


def grouped() -> list[tuple[str, list[Operation]]]:
    """Return the operations bucketed by group, in the order the window lists them."""
    buckets: dict[str, list[Operation]] = {name: [] for name in GROUP_ORDER}
    for operation in OPERATIONS:
        buckets[operation.group].append(operation)
    return [(name, buckets[name]) for name in GROUP_ORDER if buckets[name]]


def loaders_for(set_key: str) -> list[Operation]:
    """Return every operation that can fill the given enumeration set."""
    return [op for op in OPERATIONS if op.loads is not None and op.loads.set_key == set_key]


def fields_using(set_key: str) -> list[tuple[Operation, Input]]:
    """Return every body field that draws on the given enumeration set."""
    found: list[tuple[Operation, Input]] = []
    for operation in OPERATIONS:
        for item in operation.body:
            if item.enum_set == set_key:
                found.append((operation, item))
    return found
