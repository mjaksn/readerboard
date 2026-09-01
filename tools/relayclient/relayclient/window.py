"""The one screen.

Everything the tool does is on it: the connection, the enumerations, all twenty
operations, the form for whichever one is selected, and the response. Nothing is
more than one click away, and the things that would need a quarter of the window
to show properly open as dialogs instead.

The forms are built from :mod:`relayclient.catalogue` rather than written out
one by one, so "it can call any endpoint" is a property of the table rather than
a claim about the window.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QGuiApplication
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
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from relayclient import catalogue, enums, skew
from relayclient import format as fmt
from relayclient import request as request_module
from relayclient.catalogue import Input, Operation
from relayclient.dialogs import CurlPreview, EntryPicker, ErrorDialog, HistoryDialog
from relayclient.history import History
from relayclient.net import Caller, Completed, DescriptionFetcher

DEFAULT_BASE_URL = "http://127.0.0.1:8000"

MUTED = "color:#7a746a"


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

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "Nothing is loaded until you ask for it. Load a set and it becomes "
            "available in the fields that use it."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(MUTED)
        layout.addWidget(intro)

        for set_key in catalogue.SET_ORDER:
            layout.addWidget(self._build_row(set_key))

        layout.addStretch(1)

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
            # Two endpoints can fill the same set. Both are offered, because the
            # tool has to be able to call either, and the label says which.
            label = "Load" if operation.group == "Enumerations" else "Load (simple)"
            button = QPushButton(label)
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

        status = QLabel("not loaded")
        status.setStyleSheet(MUTED)
        status.setWordWrap(True)
        self._status[set_key] = status
        rows.addWidget(status)

        return holder

    def refresh(self) -> None:
        """Update every row from what the store now holds."""
        for set_key, label in self._status.items():
            loaded = self._window.store.get(set_key)
            if loaded is None:
                label.setText("not loaded")
                label.setStyleSheet(MUTED)
                self._view[set_key].setEnabled(False)
            else:
                label.setText(loaded.summary())
                label.setStyleSheet("color:%s" % fmt.OK_COLOUR)
                self._view[set_key].setEnabled(True)

    def _show(self, set_key: str) -> None:
        """Open the set for reading."""
        loaded = self._window.store.get(set_key)
        if loaded is None:
            return
        EntryPicker(
            catalogue.SET_TITLES[set_key], loaded.entries, parent=self._window
        ).exec()


class OperationForm(QWidget):
    """The inputs for one operation, generated from its catalogue entry."""

    def __init__(self, operation: Operation, window: MainWindow) -> None:
        """Build the fields this operation takes."""
        super().__init__()
        self.operation = operation
        self._window = window
        self._path: dict[str, QWidget] = {}
        self._body: dict[str, QWidget] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        heading = QLabel("%s\n%s" % (operation.summary, operation.signature))
        heading.setStyleSheet("font-weight:600")
        layout.addWidget(heading)

        if operation.note:
            note = QLabel(operation.note)
            note.setWordWrap(True)
            note.setStyleSheet(MUTED)
            layout.addWidget(note)

        if operation.needs_key:
            key_note = QLabel("Sends the X-API-Key header.")
            key_note.setStyleSheet(MUTED)
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
            empty.setStyleSheet(MUTED)
            layout.addWidget(empty)

        layout.addStretch(1)

    def _label(self, item: Input, where: str) -> QLabel:
        """Return the caption for a field, marking the required ones."""
        text = item.label + (" *" if item.required else "")
        label = QLabel(text)
        label.setToolTip("%s (%s)" % (item.description or item.name, where))
        return label

    def _path_widget(self, item: Input) -> QWidget:
        """Build a path parameter, with the slot key loader when it is one."""
        if not item.slot_keys:
            edit = QLineEdit()
            edit.setPlaceholderText(item.description)
            self._path[item.name] = edit
            return edit

        combo = QComboBox()
        combo.setEditable(True)
        _placeholder(combo, item.description)
        self._path[item.name] = combo

        load = QPushButton("Load keys")
        load.setToolTip("Call GET /v2/messages and offer the keys it returns")
        load.clicked.connect(self._window.load_slot_keys)

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
            self._fill_combo(item, combo)
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
                lambda _checked=False, name=item.name: self._insert_token(name)
            )
            self._token_buttons = getattr(self, "_token_buttons", [])
            self._token_buttons.append(insert)

            row = QVBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(edit)
            buttons = QHBoxLayout()
            buttons.addWidget(insert)
            buttons.addStretch(1)
            row.addLayout(buttons)
            holder = QWidget()
            holder.setLayout(row)
            self._refresh_token_button(insert)
            return holder

        line = QLineEdit()
        line.setPlaceholderText(item.description)
        if item.prefill is not None:
            line.setText(str(item.prefill))
        self._body[item.name] = line
        return line

    def _fill_combo(self, item: Input, combo: QComboBox) -> None:
        """Offer whatever has been loaded for this field's set, and say so if nothing has."""
        current = combo.currentText()
        combo.clear()
        names = self._window.store.names(item.enum_set or "")
        if names:
            combo.addItems(list(names))
            _placeholder(combo, item.description)
        else:
            _placeholder(
                combo,
                "load %s on the left to choose from a list"
                % catalogue.SET_TITLES.get(item.enum_set or "", "the set").lower(),
            )
        if current:
            combo.setCurrentText(current)
        elif item.prefill is not None:
            combo.setCurrentText(str(item.prefill))

    def _refresh_token_button(self, button: QPushButton) -> None:
        """Enable the token inserter only once the tokens are actually loaded."""
        loaded = self._window.store.is_loaded(catalogue.MARKUP_TOKENS)
        button.setEnabled(loaded)
        button.setToolTip(
            "Choose from the loaded markup tokens"
            if loaded
            else "Load the markup tokens on the left first"
        )

    def _insert_token(self, field_name: str) -> None:
        """Ask for a token and put it where the cursor is."""
        loaded = self._window.store.get(catalogue.MARKUP_TOKENS)
        if loaded is None:
            return
        picker = EntryPicker(
            "Markup tokens", loaded.entries, parent=self._window, insert=True
        )
        if picker.exec() and picker.selected:
            widget = self._body[field_name]
            if isinstance(widget, QPlainTextEdit):
                widget.insertPlainText(picker.selected)
                widget.setFocus()

    def refresh_enumerations(self) -> None:
        """Repopulate anything that draws on a set, after one has been loaded."""
        for item in self.operation.body:
            widget = self._body.get(item.name)
            if item.enum_set and isinstance(widget, QComboBox):
                self._fill_combo(item, widget)
        for button in getattr(self, "_token_buttons", []):
            self._refresh_token_button(button)

    def offer_slot_keys(self, keys: list[str]) -> None:
        """Fill any slot key box with the keys a message list came back with."""
        for item in self.operation.path_inputs:
            widget = self._path.get(item.name)
            if item.slot_keys and isinstance(widget, QComboBox):
                current = widget.currentText()
                widget.clear()
                widget.addItems(keys)
                widget.setCurrentText(current)

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
        self.setWindowTitle("readerboard client")
        self.resize(1280, 840)

        self.store = enums.EnumStore()
        self.history = History()
        self._caller = Caller(self)
        self._caller.completed.connect(self._completed)
        self._describer = DescriptionFetcher(self)
        self._describer.fetched.connect(self._described)
        self._describer.failed.connect(self._not_described)
        # One check per address, and only after something has answered, so that
        # an unreachable service is reported by the call that failed rather than
        # by a second complaint about a file nobody asked for.
        self._checked: set[str] = set()
        self._settings = QSettings()
        self._form: OperationForm | None = None
        self._pending_slot_keys = False
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
        health.setToolTip("GET /health, the one call that needs no key")
        health.clicked.connect(lambda: self.run(catalogue.BY_ID["health"]))

        history = QPushButton("History")
        history.clicked.connect(lambda: HistoryDialog(self.history, parent=self).exec())

        self.surface = QLabel("")
        self.surface.setStyleSheet(MUTED)
        self.surface.setToolTip(
            "Whether the service's own description matches the surface this client "
            "was built for. Checked once per address."
        )

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
        self.status_strip.setStyleSheet(
            "padding:7px 10px;border-radius:4px;background:#efebe3;color:#56514a"
        )

        self.response = QTextBrowser()
        self.response.setHtml(
            "<div style='font-family:sans-serif;color:#7a746a'>"
            "Pick an operation, fill anything it needs and press Send."
            "</div>"
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
            self._form.deleteLater()

        self._form = OperationForm(catalogue.BY_ID[operation_id], self)
        self._form_holder.addWidget(self._form)

    # == sending ==========================================================

    def _prepare(self, operation: Operation) -> request_module.Prepared | None:
        """Build the request for an operation, reporting what stopped it if anything did."""
        form = self._form
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
        """Send whichever operation is selected."""
        if self._form is None:
            return
        self.run(self._form.operation)

    def run(self, operation: Operation) -> None:
        """Send one operation, confirming first if it clears the whole sign."""
        if self._caller.busy:
            return

        if operation.destructive:
            answer = QMessageBox.question(
                self,
                "Clear every message?",
                "%s takes every message off the sign at once. Send it?" % operation.signature,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        prepared = self._prepare(operation)
        if prepared is None:
            return

        self._settings.setValue("base_url", self.base_url.text().strip())
        self._started_at = datetime.now()
        self.send_button.setEnabled(False)
        self._set_strip("Sending %s ..." % operation.signature, None)
        self._caller.send(prepared)

    def load_slot_keys(self) -> None:
        """Fetch the message list so the key boxes can offer what is registered."""
        self._pending_slot_keys = True
        self.run(catalogue.BY_ID["list_messages"])

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
        self.response.setHtml(fmt.as_html(rendered))

        if ok:
            self._absorb(operation, payload)
        else:
            # Required: every failure opens with its full content, not just a colour.
            ErrorDialog(rendered, parent=self).exec()

        self._check_surface()

        if self._pending_slot_keys and operation.id == "list_messages":
            self._pending_slot_keys = False
            if ok and isinstance(payload, list):
                keys = [
                    str(item["key"])
                    for item in payload
                    if isinstance(item, dict) and "key" in item
                ]
                if self._form is not None:
                    self._form.offer_slot_keys(keys)

    def _absorb(self, operation: Operation, payload: object) -> None:
        """Take an enumeration into the store, if that is what just came back."""
        if operation.loads is None:
            return
        try:
            entries = enums.parse(payload, operation.loads.name_field)
        except enums.MalformedEnumeration as err:
            QMessageBox.warning(
                self,
                "That did not look like an enumeration",
                "%s answered something this client could not read as a set: %s"
                % (operation.signature, err),
            )
            return

        self.store.load(operation.loads.set_key, operation.signature, entries)
        self.enumerations.refresh()
        if self._form is not None:
            self._form.refresh_enumerations()

    def _check_surface(self) -> None:
        """Ask this address to describe itself, the first time it answers anything."""
        try:
            address = request_module.normalise_base_url(self.base_url.text())
        except request_module.InvalidRequest:
            return
        if address in self._checked:
            return
        self._checked.add(address)
        self._describer.fetch(address)

    def _described(self, document: object) -> None:
        """Say whether the service's surface is the one this client was built for."""
        try:
            difference = skew.compare(document, catalogue.OPERATIONS)
        except skew.UnreadableDescription as err:
            self._not_described(str(err))
            return

        self.surface.setText(difference.summary())
        self.surface.setToolTip(difference.detail())
        if difference.matches:
            self.surface.setStyleSheet(MUTED)
            return

        self.surface.setStyleSheet("color:%s;font-weight:600" % fmt.BAD_COLOUR)
        QMessageBox.warning(
            self,
            "This service is not the one this client was built for",
            difference.detail(),
        )

    def _not_described(self, reason: str) -> None:
        """Say that the surface could not be checked, without making a fuss of it."""
        self.surface.setText("surface not checked: %s" % reason)
        self.surface.setToolTip(
            "The service did not hand over %s, so this client cannot tell whether the "
            "surface it offers is the one the service has. Calls are unaffected."
            % skew.DESCRIPTION_PATH
        )
        self.surface.setStyleSheet(MUTED)

    def _set_strip(self, text: str, ok: bool | None) -> None:
        """Colour the status strip by outcome, or neutrally while in flight."""
        if ok is None:
            background, colour = "#efebe3", "#56514a"
        elif ok:
            background, colour = "#e2f1ea", fmt.OK_COLOUR
        else:
            background, colour = "#f7e4da", fmt.BAD_COLOUR
        self.status_strip.setStyleSheet(
            "padding:7px 10px;border-radius:4px;background:%s;color:%s;font-weight:600"
            % (background, colour)
        )
        self.status_strip.setText(text)
