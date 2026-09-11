"""The one screen.

Everything the tool does is on it: the connection, the enumerations, all twenty-one
operations, the form for whichever one is selected, and the response. Nothing is
more than one click away, and the things that would need a quarter of the window
to show properly open as dialogs instead.

The forms are built from :mod:`apiclient.catalogue` rather than written out
one by one, so "it can call any endpoint" is a property of the table rather than
a claim about the window.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QMargins, QSettings, Qt
from PySide6.QtGui import QGuiApplication, QPalette
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from apiclient import catalogue, enums, names, skew
from apiclient import format as fmt
from apiclient import request as request_module
from apiclient.catalogue import Input, Operation
from apiclient.dialogs import (
    NOTHING_ANSWERED,
    SERVICE_PROBLEM,
    CurlPreview,
    EntryPicker,
    ErrorDialog,
    HistoryDialog,
)
from apiclient.history import History
from apiclient.net import Caller, Completed, DescriptionFetcher

DEFAULT_BASE_URL = "http://127.0.0.1:5001"

# What the surface label means, held here rather than set once in the
# constructor. _show_verdict rewrites the tooltip on every keystroke, so a
# sentence that existed only where the label was built would be gone after the
# first one and nothing would ever put it back.
SURFACE_TOOLTIP = (
    "Whether the service's own description matches the surface this client was "
    "built for. Checked once per address."
)


# How much of the screen to leave clear around a window when it first appears,
# in logical pixels. Enough to say that it is a window and not a maximised one,
# and no more, because on a small desktop every pixel comes out of the panes.
SCREEN_MARGIN = 24


def fit_to_screen(window: QWidget, margin: int = SCREEN_MARGIN) -> None:
    """Shrink the window to fit its screen with a margin all round, then centre it.

    The size set before this is called is the one the window would like, and it
    keeps it when the screen has room. A scaled desktop often has not: a 3840
    by 2400 panel at 300% is 1280 by 800 logical pixels, less the taskbar, and
    a window built to a fixed size on it comes up with its bottom edge behind
    the taskbar, looking right only once it is maximised.

    The frame counts towards what has to fit, and Qt does not know how tall the
    title bar is until the native window exists, so this creates it first.
    ``move`` places the frame's corner while ``resize`` sets the client area,
    which is why the two are worked out separately.

    The sign simulator carries the same function, for the reason it carries its
    own lock file: the two tools share nothing, so that one can move without
    the other.
    """
    edge = QMargins(margin, margin, margin, margin)
    room = window.screen().availableGeometry().marginsRemoved(edge)
    window.winId()
    frame = window.windowHandle().frameMargins()
    window.resize(window.size().boundedTo(room.marginsRemoved(frame).size()))
    outer = window.size().grownBy(frame)
    window.move(
        room.left() + (room.width() - outer.width()) // 2,
        room.top() + (room.height() - outer.height()) // 2,
    )


def _muted(theme: fmt.Theme) -> str:
    """Return the stylesheet for text that should recede, in the theme in use.

    A constant would be one theme's colour written down, and a stylesheet beats
    the palette: the label would stay dark grey on a dark ground instead of
    taking the light ink the palette had already chosen for it.
    """
    return "color:%s" % theme.muted


class EnumerationPanel(QGroupBox):
    """The four sets, each empty until somebody presses its button.

    This is the only way the client learns the vocabulary. Nothing is compiled
    in, so what the fields offer is always what this service answered, not what
    some earlier version of it did.
    """

    def __init__(self, window: MainWindow) -> None:
        """Build a row for each set the catalogue knows about."""
        super().__init__("Enumerations")
        self._window = window
        self._status: dict[str, QLabel] = {}
        self._view: dict[str, QPushButton] = {}

        # The rows sit in a scroll area so that this panel's height is not the
        # window's minimum. The four rows and the intro need about 435 pixels,
        # and with the response pane and the connection strip that put the
        # window's minimum past what a 1080p desktop at 150% has above the
        # taskbar. The bar appears only when the panel is squeezed below what
        # the rows need, so on a desktop with room this looks as it did.
        rows = QWidget()
        layout = QVBoxLayout(rows)
        layout.setSpacing(10)
        layout.setContentsMargins(0, 0, 0, 0)

        intro = QLabel(
            "Nothing is loaded until you ask for it. Load a set and it becomes "
            "available in the fields that use it."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(_muted(self._window.theme))
        layout.addWidget(intro)

        for set_key in catalogue.SET_ORDER:
            layout.addWidget(self._build_row(set_key))

        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # A scroll area paints its viewport on the base colour, which would put
        # a white sheet behind the rows on a light desktop and a black one on a
        # dark desktop. The panel's own ground is the right one.
        scroll.viewport().setBackgroundRole(QPalette.ColorRole.Window)
        scroll.setWidget(rows)
        outer = QVBoxLayout(self)
        outer.addWidget(scroll)

    def _build_row(self, set_key: str) -> QWidget:
        """Build the title, buttons and status line for one set."""
        holder = QFrame()
        holder.setFrameShape(QFrame.Shape.StyledPanel)
        rows = QVBoxLayout(holder)
        rows.setSpacing(4)

        title = QLabel(catalogue.SET_TITLES[set_key])
        title.setStyleSheet("font-weight:600")
        rows.addWidget(title)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for operation in catalogue.loaders_for(set_key):
            button = QPushButton("Load")
            button.setToolTip(operation.signature)
            button.clicked.connect(
                lambda _checked=False, op=operation: self._window.run(op)
            )
            buttons.addWidget(button)

        view = QPushButton("View")
        view.setEnabled(False)
        view.clicked.connect(lambda _checked=False, key=set_key: self._show(key))
        self._view[set_key] = view
        buttons.addWidget(view)
        buttons.addStretch(1)
        rows.addLayout(buttons)

        # No word wrap, on purpose. A wrapping label reports a one-line minimum
        # height, so when the top pane is shorter than this panel wants, the
        # layout shrinks a wrapped status to one line and clips it. The text is
        # kept short enough to fit instead, with the endpoint on the tooltip.
        status = QLabel("not loaded")
        status.setStyleSheet(_muted(self._window.theme))
        self._status[set_key] = status
        rows.addWidget(status)

        return holder

    def refresh(self) -> None:
        """Update every row from what the store now holds."""
        for set_key, label in self._status.items():
            loaded = self._window.store.get(set_key)
            if loaded is None:
                label.setText("not loaded")
                label.setToolTip("")
                label.setStyleSheet(_muted(self._window.theme))
                self._view[set_key].setEnabled(False)
            else:
                label.setText(loaded.summary())
                label.setToolTip(loaded.provenance())
                label.setStyleSheet("color:%s" % self._window.theme.ok)
                self._view[set_key].setEnabled(True)

    def _show(self, set_key: str) -> None:
        """Open the set for reading."""
        loaded = self._window.store.get(set_key)
        if loaded is None:
            return
        picker = EntryPicker(
            catalogue.SET_TITLES[set_key], loaded.entries, parent=self._window
        )
        picker.exec()
        # Parented to the window, so nothing else will ever drop it. One per
        # press would otherwise be held, with its whole table, until the run
        # ended. EntryPicker cannot delete itself on close because the other
        # call site reads its choice after exec returns.
        picker.deleteLater()


class OperationForm(QWidget):
    """The inputs for one operation, generated from its catalogue entry."""

    def __init__(self, operation: Operation, window: MainWindow) -> None:
        """Build the fields this operation takes."""
        super().__init__()
        self.operation = operation
        self._window = window
        self._path: dict[str, QWidget] = {}
        self._body: dict[str, QWidget] = {}
        self._token_buttons: list[tuple[QPushButton, str]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        heading = QLabel("%s\n%s" % (operation.summary, operation.signature))
        heading.setStyleSheet("font-weight:600")
        layout.addWidget(heading)

        if operation.note:
            note = QLabel(operation.note)
            note.setWordWrap(True)
            note.setStyleSheet(_muted(self._window.theme))
            layout.addWidget(note)

        if operation.needs_key:
            key_note = QLabel("Sends the X-API-Key header.")
            key_note.setStyleSheet(_muted(self._window.theme))
            layout.addWidget(key_note)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        for item in operation.path_inputs:
            form.addRow(self._label(item, "path"), self._path_widget(item))
        for item in operation.body:
            form.addRow(self._label(item, "body"), self._body_widget(item))

        layout.addLayout(form)

        if not operation.path_inputs and not operation.body:
            empty = QLabel("This one takes nothing. Press Send.")
            empty.setStyleSheet(_muted(self._window.theme))
            layout.addWidget(empty)

        layout.addStretch(1)

    def _label(self, item: Input, where: str) -> QWidget:
        """Return the caption for a field, marking the required ones.

        A field with ``fill_from`` gets its loader button here, under the
        caption, rather than beside the field. The field's own row is already
        spoken for: a markup textarea carries Insert token beneath it, and a
        second button there would read as another way to edit the text rather
        than a way to replace all of it.
        """
        text = item.label + (" *" if item.required else "")
        label = QLabel(text)
        label.setToolTip("%s (%s)" % (item.description or item.name, where))
        if not item.fill_from:
            return label

        source = catalogue.BY_ID[item.fill_from]
        # Two lines, and it is the width that wants them rather than the
        # wording. This button sits in the form's label column, so on one line
        # it became the widest thing there and pushed every field right by the
        # difference. Broken in two it is narrower than "display mode" below
        # it, which was already setting that width, so the column is exactly as
        # wide as it would be if this button were not here. The row is tall
        # enough for the second line at no cost, because the message field
        # beside it is a textarea.
        load = QPushButton("Load From\nSign")
        load.setToolTip(
            "Call %s for the key above and fill this form from what comes back"
            % source.signature
        )
        load.clicked.connect(
            lambda _checked=False, op=item.fill_from: self._window.load_from_sign(op)
        )

        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.addWidget(label)
        column.addWidget(load)
        column.addStretch(1)
        holder = QWidget()
        holder.setLayout(column)
        return holder

    def _path_widget(self, item: Input) -> QWidget:
        """Build a path parameter, with a loader for the keys it can take when it has one."""
        if not item.keys_from:
            edit = QLineEdit()
            edit.setPlaceholderText(item.description)
            self._path[item.name] = edit
            return edit

        combo = QComboBox()
        combo.setEditable(True)
        _placeholder(combo, item.description)
        self._path[item.name] = combo

        load = QPushButton("Load keys")
        load.setToolTip(
            "Call %s and offer the keys it returns"
            % catalogue.BY_ID[item.keys_from].signature
        )
        load.clicked.connect(
            lambda _checked=False, op=item.keys_from: self._window.load_keys(op)
        )

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(combo, 1)
        row.addWidget(load)
        holder = QWidget()
        holder.setLayout(row)
        return holder

    def _body_widget(self, item: Input) -> QWidget:
        """Build a body field of whichever kind the catalogue says it is."""
        if item.enum_set:
            combo = QComboBox()
            combo.setEditable(True)
            self._body[item.name] = combo
            self._fill_combo(item, combo, initial=True)
            return combo

        if item.kind == "textarea":
            edit = QPlainTextEdit()
            edit.setPlaceholderText(item.description)
            edit.setMinimumHeight(70)
            self._body[item.name] = edit
            if not item.markup:
                return edit

            insert = QPushButton("Insert token")
            insert.clicked.connect(
                lambda _checked=False, name=item.name, tokens=item.markup: self._insert_token(
                    name, tokens
                )
            )
            self._token_buttons.append((insert, item.markup))

            row = QVBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(edit)
            buttons = QHBoxLayout()
            buttons.addWidget(insert)
            buttons.addStretch(1)
            row.addLayout(buttons)
            holder = QWidget()
            holder.setLayout(row)
            self._refresh_token_button(insert, item.markup)
            return holder

        line = QLineEdit()
        line.setPlaceholderText(item.description)
        if item.prefill is not None:
            line.setText(str(item.prefill))
        self._body[item.name] = line
        return line

    def _fill_combo(self, item: Input, combo: QComboBox, *, initial: bool = False) -> None:
        """Offer whatever has been loaded for this field's set, and say so if nothing has.

        Loading a set must not answer the question for the caller. ``addItems``
        selects the first entry, so without the restore below, loading the
        control commands would leave the command box holding the first one and
        Send would fire a command nobody picked.

        The prefill is applied only when the field is first built. Refreshing
        happens whenever any set is loaded, and putting the prefill back then
        would undo a box somebody had emptied on purpose, which is the one thing
        clearing a required field is for.
        """
        current = combo.currentText()
        combo.clear()
        names = self._window.store.names(item.enum_set or "")
        if names:
            combo.addItems(list(names))

        if current:
            combo.setCurrentText(current)
        elif initial and item.prefill is not None:
            combo.setCurrentText(str(item.prefill))
        else:
            combo.setCurrentIndex(-1)

        if names:
            _placeholder(combo, item.description)
        else:
            _placeholder(
                combo,
                "load %s on the left to choose from a list"
                % catalogue.SET_TITLES.get(item.enum_set or "", "the set").lower(),
            )

    def _refresh_token_button(self, button: QPushButton, tokens: str) -> None:
        """Enable the token inserter only once its tokens are actually loaded."""
        loaded = self._window.store.is_loaded(tokens)
        title = catalogue.SET_TITLES[tokens].lower()
        button.setEnabled(loaded)
        button.setToolTip(
            "Choose from the loaded %s" % title
            if loaded
            else "Load the %s on the left first" % title
        )

    def _insert_token(self, field_name: str, tokens: str) -> None:
        """Ask for a token from the field's own set and put it where the cursor is."""
        loaded = self._window.store.get(tokens)
        if loaded is None:
            return
        picker = EntryPicker(
            catalogue.SET_TITLES[tokens], loaded.entries, parent=self._window, insert=True
        )
        chosen = picker.selected if picker.exec() else None
        # Read first, dropped second. The read is why this one cannot carry
        # WA_DeleteOnClose the way the other dialogs do.
        picker.deleteLater()
        if chosen:
            widget = self._body[field_name]
            if isinstance(widget, QPlainTextEdit):
                widget.insertPlainText(chosen)
                widget.setFocus()

    def refresh_enumerations(self) -> None:
        """Repopulate anything that draws on a set, after one has been loaded."""
        for item in self.operation.body:
            widget = self._body.get(item.name)
            if item.enum_set and isinstance(widget, QComboBox):
                self._fill_combo(item, widget)
        for button, tokens in self._token_buttons:
            self._refresh_token_button(button, tokens)

    def offer_keys(self, listed_by: str, entries: list[object]) -> None:
        """Fill the key box a list operation serves with the keys it came back with.

        Each entry carries its key under the parameter's own name, ``key`` for
        a message and ``name`` for a variable, which is what the catalogue's
        ``keys_from`` relies on.

        Nothing is chosen for the caller. Leaving the first key selected would
        make Load followed by Send act on a message at random, which for the
        delete beside this one is the worst version of that mistake.

        A key already typed is kept only if the sign turned out to have it. The
        list is the answer to "what is registered", so a key missing from it is
        one no request in this form can succeed with: the read would 404 and the
        delete would too. Clearing it says that at the moment it becomes known,
        rather than leaving it sitting there looking as valid as it did before
        the list arrived.

        What is compared is the key with the whitespace around it trimmed,
        because that is what this client sends: ``request.fill_path`` trims
        every path value on its way out, and the service would refuse the
        untrimmed one anyway. So ``  porch `` is the key ``porch`` as far as any
        request is concerned, and on a match the box is rewritten to the
        trimmed form, so that what it shows is the key the sign actually has.
        Case is not folded: slot keys are case sensitive, and ``Porch`` is a
        different slot that the sign does not have.
        """
        for item in self.operation.path_inputs:
            widget = self._path.get(item.name)
            if item.keys_from == listed_by and isinstance(widget, QComboBox):
                keys = [
                    str(entry[item.name])
                    for entry in entries
                    if isinstance(entry, dict) and item.name in entry
                ]
                wanted = widget.currentText().strip()
                widget.clear()
                widget.addItems(keys)
                if wanted and wanted in keys:
                    widget.setCurrentText(wanted)
                else:
                    widget.setCurrentIndex(-1)
                    widget.setCurrentText("")

    def trim_path_values(self) -> None:
        """Trim the whitespace from the path boxes in place.

        ``request.fill_path`` trims every path value on its way out, so what
        reaches the service is already the trimmed key whatever the box shows.
        Doing it to the box as well makes the two agree: the key on screen after
        Send is the key that was used, rather than the one the user typed and
        the client quietly changed.
        """
        for widget in self._path.values():
            text = _text_of(widget)
            if text != text.strip():
                _set_text(widget, text.strip())

    def fill_from(self, payload: dict[str, object]) -> None:
        """Put a stored resource into the body fields, for editing rather than retyping.

        Only fields the response carries a value for are touched, so a response
        that omits one leaves whatever was typed there alone.

        A duration field is the exception, because the service answers in the
        other unit: it takes ``ttl_seconds`` from now and reports ``expires_at``,
        the moment itself. The remaining time is worked out here so that sending
        the form straight back keeps roughly the deadline the message already
        had, rather than the deadline being quietly dropped.
        """
        for item in self.operation.body:
            widget = self._body.get(item.name)
            if widget is None:
                continue
            if item.seconds_until:
                if item.seconds_until in payload:
                    _set_text(widget, _remaining(payload[item.seconds_until]))
                continue
            if item.name not in payload:
                continue
            value = payload[item.name]
            _set_text(widget, "" if value is None else str(value))

    def path_values(self) -> dict[str, str]:
        """Return what has been typed into the path parameters."""
        return {name: _text_of(widget) for name, widget in self._path.items()}

    def body_values(self) -> dict[str, str]:
        """Return what has been typed into the body fields."""
        return {name: _text_of(widget) for name, widget in self._body.items()}


def _placeholder(combo: QComboBox, text: str) -> None:
    """Set a combo box's placeholder, which only exists once it is editable."""
    line = combo.lineEdit()
    if line is not None:
        line.setPlaceholderText(text)


def _trimmed(values: dict[str, str]) -> dict[str, str]:
    """Return path values as they are sent, with the whitespace around each trimmed.

    Used where a reply is matched back to the question that produced it, so the
    comparison is between what went out and what the box would send now. A key
    the user padded with a space while the call was out is still the same key.
    """
    return {name: value.strip() for name, value in values.items()}


def _remaining(expires_at: object) -> str:
    """Return the seconds left before a deadline, as a duration field wants it.

    Empty for the three cases that cannot be written as a positive number of
    seconds ahead: no deadline at all, one this could not read, and one that has
    already passed. Empty is also what the field means by "no deadline", which
    is the honest answer for the first and the only available one for the other
    two, and the response panel shows the timestamp either way.

    Whole seconds, because the box is something a person reads and edits, and
    because the round trip that carried the answer here already cost more
    precision than the fraction would have carried.
    """
    remaining = fmt.seconds_until(expires_at)
    if remaining is None or remaining < 1:
        return ""
    return str(round(remaining))


def _set_text(widget: QWidget, value: str) -> None:
    """Put text into a field widget, whichever kind it is. The mirror of ``_text_of``."""
    if isinstance(widget, QPlainTextEdit):
        widget.setPlainText(value)
    elif isinstance(widget, QComboBox):
        widget.setCurrentText(value)
    elif isinstance(widget, QLineEdit):
        widget.setText(value)


def _text_of(widget: QWidget) -> str:
    """Return whatever a field widget currently holds, as text."""
    if isinstance(widget, QPlainTextEdit):
        return widget.toPlainText()
    if isinstance(widget, QComboBox):
        return widget.currentText()
    if isinstance(widget, QLineEdit):
        return widget.text()
    return ""


class MainWindow(QMainWindow):
    """The window, and the small amount of wiring that holds the parts together."""

    def __init__(self) -> None:
        """Build the screen and restore the base URL from last time."""
        super().__init__()
        self.setWindowTitle(names.DISPLAY_NAME)
        self.resize(1280, 840)
        fit_to_screen(self)

        # QTextBrowser and the labels paint on the palette's own ground, so the
        # ink has to come from the same place or a dark desktop reads grey on
        # dark. Decided once here rather than guessed per render.
        ground = self.palette().color(QPalette.ColorRole.Base)
        self.theme = fmt.DARK if ground.lightness() < 128 else fmt.LIGHT

        self.store = enums.EnumStore()
        self.history = History()
        self._caller = Caller(self)
        self._caller.completed.connect(self._completed)
        # One check per address, and only after something has answered, so that
        # an unreachable service is reported by the call that failed rather than
        # by a second complaint about a file nobody asked for. Declared before
        # the connections below, which reach into it.
        self._checked: set[str] = set()
        self._describer = DescriptionFetcher(self)
        self._describer.fetched.connect(self._described)
        self._describer.failed.connect(self._not_described)
        self._describer.superseded.connect(self._release)
        self._settings = QSettings()
        self._form: OperationForm | None = None
        # What was found at each address, so that editing the box shows the
        # verdict for whatever is in it rather than losing the last one. A
        # checked address is never fetched twice, so a verdict thrown away on a
        # keystroke would not come back at all.
        self._verdicts: dict[str, tuple[str, str, str]] = {}
        self._pending_keys: tuple[OperationForm, str] | None = None
        self._pending_fill: tuple[OperationForm, str, dict[str, str]] | None = None
        self._started_at = datetime.now()

        self.setCentralWidget(self._build())
        self._select_first()

    # == building =========================================================

    def _build(self) -> QWidget:
        """Assemble the whole screen."""
        root = QVBoxLayout()
        root.addWidget(self._build_top())

        columns = QSplitter(Qt.Orientation.Horizontal)

        self.enumerations = EnumerationPanel(self)
        columns.addWidget(self.enumerations)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Operation"])
        self._tree.setColumnCount(1)
        for group, operations in catalogue.grouped():
            parent = QTreeWidgetItem([group])
            parent.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for operation in operations:
                child = QTreeWidgetItem([operation.summary])
                child.setToolTip(0, operation.signature)
                child.setData(0, Qt.ItemDataRole.UserRole, operation.id)
                parent.addChild(child)
            self._tree.addTopLevelItem(parent)
        self._tree.expandAll()
        self._tree.currentItemChanged.connect(lambda *_args: self._selected())
        columns.addWidget(self._tree)

        columns.addWidget(self._build_form_side())
        columns.setSizes([300, 330, 620])

        vertical = QSplitter(Qt.Orientation.Vertical)
        vertical.addWidget(columns)
        vertical.addWidget(self._build_response())
        vertical.setSizes([520, 300])
        root.addWidget(vertical, 1)

        holder = QWidget()
        holder.setLayout(root)
        return holder

    def _build_top(self) -> QWidget:
        """Build the connection strip."""
        self.base_url = QLineEdit(
            str(self._settings.value("base_url", DEFAULT_BASE_URL))
        )
        self.base_url.setToolTip("where the service is listening")

        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("X-API-Key, never saved to disk")
        self.api_key.setToolTip(
            "Sent with the writes that need it. It is not stored between runs and "
            "is redacted everywhere it would otherwise be written down."
        )

        health = QPushButton("Health")
        health.setToolTip("GET /health, which like every read needs no key")
        health.clicked.connect(lambda: self.run(catalogue.BY_ID["health"]))

        history = QPushButton("History")
        history.clicked.connect(
            lambda: HistoryDialog(self.history, parent=self, theme=self.theme).exec()
        )

        self.surface = QLabel("")
        self.surface.setStyleSheet(_muted(self.theme))
        self.surface.setToolTip(SURFACE_TOOLTIP)
        # Connected here rather than beside the box it watches, because the slot
        # touches the label above and a signal wired before its target exists is
        # a crash waiting for the first line of code that happens to fire it.
        self.base_url.textChanged.connect(self._show_verdict)

        row = QHBoxLayout()
        row.addWidget(QLabel("Base URL"))
        row.addWidget(self.base_url, 2)
        row.addWidget(QLabel("API key"))
        row.addWidget(self.api_key, 1)
        row.addWidget(health)
        row.addWidget(history)

        outer = QVBoxLayout()
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(row)
        outer.addWidget(self.surface)

        holder = QWidget()
        holder.setLayout(outer)
        return holder

    def _build_form_side(self) -> QWidget:
        """Build the pane the generated form lives in."""
        self._form_holder = QVBoxLayout()
        self._form_holder.setContentsMargins(0, 0, 0, 0)

        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send_current)

        curl = QPushButton("Copy as curl")
        curl.setToolTip("The same call as a curl command, with the key as a variable")
        curl.clicked.connect(self._copy_curl)

        buttons = QHBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addWidget(curl)
        buttons.addStretch(1)

        outer = QVBoxLayout()
        outer.addLayout(self._form_holder, 1)
        outer.addLayout(buttons)

        holder = QWidget()
        holder.setLayout(outer)
        return holder

    def _build_response(self) -> QWidget:
        """Build the status strip and the formatted response below it."""
        self.status_strip = QLabel("No call made yet.")
        self.status_strip.setStyleSheet("padding:7px 10px;border-radius:4px")

        self.response = QTextBrowser()
        self.response.setHtml(
            "<div style='font-family:sans-serif;color:%s'>"
            "Pick an operation, fill anything it needs and press Send."
            "</div>" % self.theme.muted
        )

        layout = QVBoxLayout()
        layout.addWidget(self.status_strip)
        layout.addWidget(self.response, 1)

        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # == selection ========================================================

    def _select_first(self) -> None:
        """Start on the health check, which is what you press first anyway."""
        top = self._tree.topLevelItem(0)
        if top is None or not top.childCount():
            return
        first = top.child(0)
        if first is not None:
            self._tree.setCurrentItem(first)

    def _selected(self) -> None:
        """Swap the form for the highlighted operation."""
        item = self._tree.currentItem()
        if item is None:
            return
        operation_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not operation_id:
            return

        if self._form is not None:
            self._form_holder.removeWidget(self._form)
            # Removing from a layout does not hide it. Until the deferred delete
            # runs it is still a child at its old geometry, overlapping the new
            # form, and whether that is ever seen depends on event loop timing
            # rather than on anything here.
            self._form.setParent(None)
            self._form.deleteLater()

        self._form = OperationForm(catalogue.BY_ID[operation_id], self)
        self._form_holder.addWidget(self._form)

    # == sending ==========================================================

    def _prepare(
        self,
        operation: Operation,
        path_values: dict[str, str] | None = None,
    ) -> request_module.Prepared | None:
        """Build the request for an operation, reporting what stopped it if anything did.

        ``path_values`` is given when the operation being sent is not the one on
        screen, which is how a form borrows another operation's read of the same
        resource. The body is still taken from the form only when the form is
        this operation's, because another operation's fields are not this one's.
        """
        form = self._form
        if path_values is None:
            path_values = form.path_values() if form and form.operation is operation else {}
        body_values = form.body_values() if form and form.operation is operation else {}
        try:
            return request_module.build(
                operation,
                self.base_url.text(),
                path_values=path_values,
                body_values=body_values,
                api_key=self.api_key.text(),
            )
        except request_module.InvalidRequest as err:
            QMessageBox.warning(self, "Cannot send that yet", str(err))
            return None

    def _send_current(self) -> None:
        """Send whichever operation is selected, trimming its path boxes first.

        Trimmed before the send rather than after it went out, so that a key of
        nothing but spaces shows as the empty box it is when the refusal to send
        it explains that the key cannot be empty.
        """
        if self._form is None:
            return
        self._form.trim_path_values()
        self.run(self._form.operation)

    def run(
        self,
        operation: Operation,
        *,
        path_values: dict[str, str] | None = None,
    ) -> bool:
        """Send one operation, confirming first if it is disruptive to run.

        Two operations ask first, and for different reasons: clearing every
        message throws work away, and rebooting the sign blanks it while it
        resets. See ``_confirm_disruptive`` for the warning-coloured prompt the
        second one shows.

        Returns whether the request actually went out, which is what stops a
        caller acting as though it had.
        """
        if self._caller.busy:
            # Only Send is greyed while a call is out, so every other button
            # that reaches here is still live. Saying so beats looking broken.
            self._set_strip(
                "Still waiting on the last call. %s was not sent." % operation.signature,
                None,
            )
            return False

        if operation.confirm:
            if not self._confirm_disruptive(operation):
                return False
        elif operation.destructive:
            answer = QMessageBox.question(
                self,
                "Clear every message?",
                "%s takes every message off the sign at once. Send it?" % operation.signature,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False

        prepared = self._prepare(operation, path_values)
        if prepared is None:
            return False

        self._settings.setValue("base_url", self.base_url.text().strip())
        self._started_at = datetime.now()
        self.send_button.setEnabled(False)
        self._set_strip("Sending %s ..." % operation.signature, None)
        self._caller.send(prepared)
        return True

    def _confirm_disruptive(self, operation: Operation) -> bool:
        """Ask before a disruptive operation, in warning colours. Returns yes/no.

        Distinct from the plain question a destructive delete asks. This fronts
        an operation that is not throwing work away but is disruptive to run,
        such as rebooting the sign, so it wears the theme's warning ink and
        defaults to No. The wording is the operation's own ``confirm`` text, so
        the catalogue carries what is said and this only paints it.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Please confirm")
        box.setText(operation.summary)
        box.setInformativeText(operation.confirm)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.setStyleSheet("QLabel { color: %s }" % self.theme.bad)
        return box.exec() == QMessageBox.StandardButton.Yes

    def load_keys(self, operation_id: str) -> None:
        """Fetch a list so the key box it serves can offer what exists.

        What is remembered is the form that asked, and it is set only once the
        request is on its way. Set before, a send that never happened would leave
        it standing, and the next message list the user asked for on their own
        account would quietly rewrite the key box under them. The form rather
        than a yes or no, because the answer belongs to the form that asked for
        it and not to whichever one is on screen when it arrives.
        """
        form = self._form
        if form is not None and self.run(catalogue.BY_ID[operation_id]):
            self._pending_keys = (form, operation_id)

    def load_from_sign(self, operation_id: str) -> None:
        """Read what is already stored under the key on screen, to edit rather than retype.

        The key is passed across explicitly, because the operation being sent is
        not the one the form is showing and the form's own values are only
        offered for its own operation.

        What is remembered is the form object, not its name. ``_selected``
        builds a fresh form on every swap, so identity is what says the answer
        is still going to the fields that asked for it, and a form that has
        since been replaced is one this must not write into. The key asked for
        is remembered with it for the same reason: a reply is matched to the
        question by nothing but arriving next, so a key retyped while the call
        was out would otherwise be answered with the previous key's message.
        """
        form = self._form
        if form is None:
            return
        asked = form.path_values()
        if self.run(catalogue.BY_ID[operation_id], path_values=asked):
            self._pending_fill = (form, operation_id, _trimmed(asked))

    def _copy_curl(self) -> None:
        """Put the current form's call on the clipboard as a curl command."""
        if self._form is None:
            return
        prepared = self._prepare(self._form.operation)
        if prepared is None:
            return
        command = request_module.as_curl(
            prepared.method, prepared.url, prepared.headers, prepared.body
        )
        QGuiApplication.clipboard().setText(command)
        CurlPreview(command, parent=self).exec()

    # == receiving ========================================================

    def _completed(self, result: Completed) -> None:
        """Read the response, record it, and show it."""
        self.send_button.setEnabled(True)
        operation = catalogue.BY_ID[result.prepared.operation_id]

        payload = fmt.parse_body(result.body)
        ok = not fmt.is_error(result.status, payload)
        rendered = fmt.render(operation, result.status, result.reason, result.body)

        self.history.append(
            prepared=result.prepared,
            summary=operation.summary,
            started_at=self._started_at,
            status=result.status,
            reason=result.reason,
            ok=ok,
            duration_ms=result.duration_ms,
            response_headers=result.headers,
            response_body=result.body,
        )

        self._set_strip(
            "%s  %s  %d ms" % (operation.signature, rendered.headline, result.duration_ms),
            ok,
        )
        self.response.setHtml(fmt.as_html(rendered, self.theme))

        if ok:
            self._absorb(operation, payload)
        else:
            # Required: every failure opens with its full content, not just a colour.
            # The title distinguishes the one failure where no service reported
            # anything, because nothing answered at all.
            ErrorDialog(
                rendered,
                parent=self,
                theme=self.theme,
                title=NOTHING_ANSWERED if result.status == 0 else SERVICE_PROBLEM,
            ).exec()

        if result.status:
            # Only once something has actually answered. A call that never
            # completed says nothing about the surface, and complaining twice
            # about one unreachable service helps nobody. The address is the one
            # this call went to, which is not always the one in the box now.
            self._check_surface(result.prepared.origin)

        # Whether the reply came from the service the box still names. The base
        # URL is editable while a call is out, and both loaders below write into
        # the form: a reply from the service it was aimed at a moment ago would
        # fill it with one service's message, or clear a key against another's
        # list, and Send would then act on the service the box names now.
        same_service = result.prepared.origin == self._current_address()

        pending = self._pending_fill
        if pending is not None and operation.id == pending[1]:
            form, _, asked = pending
            # Cleared whatever happened, so a refusal does not leave this armed
            # for somebody else's call to satisfy.
            self._pending_fill = None
            if (
                ok
                and same_service
                and isinstance(payload, dict)
                and self._form is form
                and _trimmed(form.path_values()) == asked
            ):
                form.fill_from(payload)

        asking = self._pending_keys
        if asking is not None and operation.id == asking[1]:
            self._pending_keys = None
            # Only to the form that asked. The tree stays live while a call is
            # out, and offering keys now clears a typed key the list does not
            # contain, so a reply landing on a form selected since would erase a
            # key typed into it by somebody who never pressed Load keys there.
            form, listed_by = asking
            if ok and same_service and isinstance(payload, list) and self._form is form:
                form.offer_keys(listed_by, payload)

    def _absorb(self, operation: Operation, payload: object) -> None:
        """Take an enumeration into the store, if that is what just came back."""
        if operation.loads is None:
            return
        try:
            entries = enums.parse(payload)
        except enums.MalformedEnumeration as err:
            QMessageBox.warning(
                self,
                "That did not look like an enumeration",
                "%s answered something this client could not read as a set: %s"
                % (operation.signature, err),
            )
            return

        self.store.load(operation.loads, operation.signature, entries)
        self.enumerations.refresh()
        if self._form is not None:
            self._form.refresh_enumerations()

    def _check_surface(self, address: str) -> None:
        """Ask an address to describe itself, the first time it answers anything."""
        if not address or address in self._checked:
            return
        self._checked.add(address)
        self._describer.fetch(address)

    def _show_verdict(self) -> None:
        """Show whatever is known about the address currently in the box."""
        # The tooltip is the label's own explanation, not a verdict, so the
        # no-verdict case restores it rather than blanking it.
        text, tooltip, style = self._verdicts.get(
            self._current_address(), ("", SURFACE_TOOLTIP, _muted(self.theme))
        )
        self.surface.setText(text)
        self.surface.setToolTip(tooltip)
        self.surface.setStyleSheet(style)

    def _remember_verdict(self, address: str, text: str, tooltip: str, style: str) -> None:
        """Record what was found at an address, and show it if that is where we are."""
        self._verdicts[address] = (text, tooltip, style)
        self._show_verdict()

    def _release(self, address: str) -> None:
        """Stop claiming an address was checked when its fetch was abandoned."""
        self._checked.discard(address)

    def _current_address(self) -> str:
        """Return the address in the box, or a blank when it is not a usable one."""
        try:
            return request_module.normalise_base_url(self.base_url.text())
        except request_module.InvalidRequest:
            return ""

    def _described(self, address: str, document: object) -> None:
        """Say whether the service's surface is the one this client was built for."""
        try:
            difference = skew.compare(document, catalogue.OPERATIONS)
        except skew.UnreadableDescription as err:
            self._not_described(address, str(err))
            return

        style = (
            _muted(self.theme)
            if difference.matches
            else "color:%s;font-weight:600" % self.theme.bad
        )
        self._remember_verdict(address, difference.summary(), difference.detail(), style)
        if difference.matches:
            return

        # Named rather than implied. A verdict can arrive after the address in
        # the box has moved on, and a dialog about neither would be worse than
        # no dialog at all.
        box = QMessageBox(self)
        # Built rather than static, so it is parented and would otherwise be
        # kept for the life of the window.
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("This service is not the one this client was built for")
        box.setText(address)
        box.setInformativeText(difference.detail())
        box.exec()

    def _not_described(self, address: str, reason: str, retryable: bool = False) -> None:
        """Say that the surface could not be checked, without making a fuss of it.

        A service that could not be reached is not a service that has been
        checked, so its address goes back in the pile and the next call that
        succeeds asks again. One that answered and had no description to give is
        left alone, because asking it again on every call would be noise.
        """
        if retryable:
            self._checked.discard(address)
            # Nothing is known about it now, so nothing should be shown for it.
            self._verdicts.pop(address, None)
            self._show_verdict()
            return
        self._remember_verdict(
            address,
            "surface not checked: %s" % reason,
            "The service did not hand over %s, so this client cannot tell whether the "
            "surface it offers is the one the service has. Calls are unaffected."
            % skew.DESCRIPTION_PATH,
            _muted(self.theme),
        )

    def _set_strip(self, text: str, ok: bool | None) -> None:
        """Colour the status strip by outcome, or neutrally while in flight."""
        if ok is None:
            background, colour = "transparent", self.theme.muted
        elif ok:
            background, colour = "transparent", self.theme.ok
        else:
            background, colour = "transparent", self.theme.bad
        self.status_strip.setStyleSheet(
            "padding:7px 10px;border-radius:4px;background:%s;color:%s;font-weight:600"
            % (background, colour)
        )
        self.status_strip.setText(text)
