"""The windows that open on top: enumerations, errors, the call history and the curl preview.

Everything here exists so the main window can stay one screen. A set of markup
tokens, the full text of an error, a network tab's worth of detail and the curl
command for the call just made are all things worth having and none of them are
worth a permanent quarter of the window, so each gets a dialog and the screen
keeps its shape.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from apiclient import catalogue
from apiclient import format as fmt
from apiclient.bitmaps import GROUND, Bitmap, tinted_tag
from apiclient.enums import Entry
from apiclient.history import History, Record
from apiclient.request import API_KEY_VARIABLE

MONO = "font-family:Consolas,'DejaVu Sans Mono',monospace;font-size:12px"

# The two things a failed call can mean, kept apart. A refused connection, an
# address that will not resolve and a transfer that timed out are the one kind
# of failure where no service reported anything, because none was reached.
SERVICE_PROBLEM = "The service reported a problem"
NOTHING_ANSWERED = "Nothing answered"

# How many pixels each dot of a thumbnail gets at a scale of one, which puts an
# icon seven dots high at a little over a line of text.
DOT_PITCH = 4


def bitmap_pixmap(bitmap: Bitmap, pitch: int = DOT_PITCH, ratio: float = 1.0) -> QPixmap:
    """Paint one icon as the dots it is, on the dark ground of the sign.

    Unlit dots are drawn rather than left out, as the sign simulator draws its
    picture files, so a narrow icon reads as narrow rather than as a gap.

    ``ratio`` is the screen's device pixel ratio. The dots are laid out in
    whole device pixels and the pixmap told its ratio afterwards, so a desktop
    scaled to 150% gets crisp dots rather than a small picture stretched.
    """
    step = max(round(pitch * ratio), 2)
    pixmap = QPixmap(max(bitmap.width, 1) * step, max(bitmap.height, 1) * step)
    pixmap.fill(QColor(GROUND))
    painter = QPainter(pixmap)
    try:
        for y in range(bitmap.height):
            for x in range(bitmap.width):
                painter.fillRect(
                    x * step, y * step, step - 1, step - 1, QColor(bitmap.colour_at(x, y))
                )
    finally:
        painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


class EntryPicker(QDialog):
    """Shows one enumeration, and optionally hands a name back to be inserted.

    The same dialog does both jobs. Opened from the enumerations panel it is a
    reference; opened from the button beside a message field it is a picker, and
    double clicking inserts.

    Given bitmaps, it draws each entry that has one in a column of its own
    before the name. An entry with none gets an empty cell, which is what a
    service older than the bitmaps endpoint looks like, so the list still works
    with nothing to draw.

    Given tints as well, it offers them in a list beside the filter. Choosing
    one redraws every icon that takes a tint in that colour and rewrites its
    name to the tag that draws it so, which is the tag Insert hands back. An
    icon drawn in its own colours is left as it is in both, because the service
    refuses a tint on one. ``tints`` is None for a set nothing tints, and empty
    for one whose tints have not been loaded, which shows the list disabled and
    says where to load them.
    """

    def __init__(
        self,
        title: str,
        entries: tuple[Entry, ...],
        *,
        parent: QWidget | None = None,
        insert: bool = False,
        bitmaps: dict[str, Bitmap] | None = None,
        tints: tuple[str, ...] | None = None,
    ) -> None:
        """Build the dialog for a set of entries."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 520)
        self.selected: str | None = None
        self._entries = entries
        self._bitmaps = bitmaps or {}
        self._insert = insert
        self._tint: str | None = None
        self._pixmaps: dict[str, QPixmap] = {}
        # Painted here and again when the tint changes, rather than on every
        # keystroke in the filter, which rebuilds the rows each time.
        self._paint()
        self._name_column = 1 if self._pixmaps else 0

        layout = QVBoxLayout(self)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("filter by name or description")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)

        self._tints: QComboBox | None = None
        if self._bitmaps and tints is not None:
            self._tints = QComboBox()
            self._tints.addItem("own colours", None)
            for word in tints:
                self._tints.addItem(word, word)
            if tints:
                self._tints.setToolTip(
                    "Draw the icons that take a tint in another colour"
                    + (", and insert them so" if insert else "")
                )
            else:
                self._tints.setEnabled(False)
                self._tints.setToolTip("Load the icon tints on the left first")
            self._tints.currentIndexChanged.connect(lambda _index: self._retint())
            top = QHBoxLayout()
            top.addWidget(self._filter, 1)
            top.addWidget(self._tints)
            layout.addLayout(top)
        else:
            layout.addWidget(self._filter)

        labels = ["name", "description"]
        if self._pixmaps:
            labels.insert(0, "")
        self._table = QTableWidget(0, len(labels))
        self._table.setHorizontalHeaderLabels(labels)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            len(labels) - 1, QHeaderView.ResizeMode.Stretch
        )
        if self._pixmaps:
            # In the pixmaps' own logical size, which is what a cell lays out by.
            sizes = [pixmap.deviceIndependentSize() for pixmap in self._pixmaps.values()]
            widest = round(max(size.width() for size in sizes))
            tallest = round(max(size.height() for size in sizes))
            self._table.setIconSize(QSize(widest, tallest))
            self._table.verticalHeader().setDefaultSectionSize(
                max(tallest + 8, self._table.verticalHeader().defaultSectionSize())
            )
        self._table.itemDoubleClicked.connect(lambda _item: self._take())
        layout.addWidget(self._table, 1)

        # Says what a chosen tint does not reach, so that an icon keeping its
        # own colours reads as the rule rather than as a fault.
        self._tint_note = QLabel("")
        self._tint_note.setWordWrap(True)
        self._tint_note.setVisible(False)
        layout.addWidget(self._tint_note)

        buttons = QDialogButtonBox()
        if insert:
            self._insert_button = buttons.addButton(
                "Insert", QDialogButtonBox.ButtonRole.AcceptRole
            )
            self._insert_button.clicked.connect(self._take)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._apply_filter("")

    def _paint(self) -> None:
        """Paint every thumbnail in the tint now chosen."""
        ratio = self.devicePixelRatioF()
        self._pixmaps = {
            name: bitmap_pixmap(bitmap.tinted(self._tint), ratio=ratio)
            for name, bitmap in self._bitmaps.items()
        }

    def _shown_name(self, entry: Entry) -> str:
        """Return the name as this list shows it, which is the tag Insert hands back."""
        bitmap = self._bitmaps.get(entry.name)
        if self._tint and bitmap is not None and bitmap.takes_tint:
            return tinted_tag(entry.name, self._tint)
        return entry.name

    def _retint(self) -> None:
        """Redraw and rename for the tint just chosen, keeping the row that was highlighted."""
        self._tint = self._tints.currentData() if self._tints is not None else None
        self._paint()
        row = self._table.currentRow()
        self._apply_filter(self._filter.text())
        if 0 <= row < self._table.rowCount():
            self._table.selectRow(row)
        if self._tint:
            if self._insert:
                note = (
                    "Icons that take a tint are shown in %s and inserted so. The rest "
                    "keep their own colours, which the service will not change, and "
                    "go in as they are." % self._tint
                )
            else:
                note = (
                    "Icons that take a tint are shown in %s. The rest keep their own "
                    "colours, which the service will not change." % self._tint
                )
            self._tint_note.setText(note)
        self._tint_note.setVisible(bool(self._tint))

    def _apply_filter(self, text: str) -> None:
        """Show only the entries matching what has been typed."""
        needle = text.strip().lower()
        matching = [
            entry
            for entry in self._entries
            if not needle
            or needle in entry.name.lower()
            or needle in entry.description.lower()
        ]
        self._table.setRowCount(len(matching))
        names = self._name_column
        for row, entry in enumerate(matching):
            if self._pixmaps:
                picture = QTableWidgetItem()
                pixmap = self._pixmaps.get(entry.name)
                if pixmap is not None:
                    picture.setData(Qt.ItemDataRole.DecorationRole, pixmap)
                self._table.setItem(row, 0, picture)
            self._table.setItem(row, names, QTableWidgetItem(self._shown_name(entry)))
            self._table.setItem(row, names + 1, QTableWidgetItem(entry.description))
        for column in range(names + 1):
            self._table.resizeColumnToContents(column)
        if matching:
            self._table.selectRow(0)

    def _take(self) -> None:
        """Accept the highlighted row as the choice."""
        item = self._table.item(self._table.currentRow(), self._name_column)
        if item is not None:
            self.selected = item.text()
            self.accept()


class ErrorDialog(QDialog):
    """The full content of a failed response, as required whenever one arrives."""

    def __init__(
        self,
        rendered: fmt.Rendered,
        *,
        parent: QWidget | None = None,
        theme: fmt.Theme = fmt.LIGHT,
        title: str = SERVICE_PROBLEM,
    ) -> None:
        """Build the dialog around an already rendered failure.

        ``title`` is a parameter because a call that never completed is the one
        failure where no service reported anything. Everything else in the tool
        keeps that apart: the headline reads "No response", the history record
        says "failed" rather than a code, and the surface check is skipped
        outright, because a call that never arrived says nothing about a service.
        """
        super().__init__(parent)
        # Modal and read only. Without this it stays a hidden child of the
        # window, holding the whole response, one per failed call.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(title)
        self.resize(680, 480)

        layout = QVBoxLayout(self)

        summary = QTextBrowser()
        summary.setHtml(fmt.as_html(rendered, theme))
        layout.addWidget(summary, 1)

        layout.addWidget(
            QLabel(
                "The response exactly as it arrived:"
                if rendered.detail
                else "There was no response to show:"
            )
        )
        raw = QTextBrowser()
        raw.setStyleSheet(MONO)
        raw.setPlainText(rendered.detail or "(no body)")
        layout.addWidget(raw, 1)

        buttons = QDialogButtonBox()
        copy = buttons.addButton("Copy response", QDialogButtonBox.ButtonRole.ActionRole)
        copy.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(rendered.detail or "")
        )
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _headers_html(title: str, headers: dict[str, str], theme: fmt.Theme) -> str:
    """Render a header block the way a network tab lists them."""
    if not headers:
        return "<div style='margin:12px 0 4px;font-weight:600'>%s</div><div>none</div>" % escape(
            title
        )
    rows = "".join(
        "<tr><td style='padding:2px 14px 2px 0;color:%s;white-space:nowrap;"
        "vertical-align:top'>%s</td><td style='padding:2px 0'>%s</td></tr>"
        % (theme.muted, escape(name), escape(value))
        for name, value in headers.items()
    )
    return (
        "<div style='margin:12px 0 4px;font-weight:600'>%s</div>"
        "<table style='border-collapse:collapse'>%s</table>" % (escape(title), rows)
    )


def _body_html(title: str, body: str | None) -> str:
    """Render a body block, exactly as it went out or came back."""
    text = body if body else ""
    shown = escape(text) if text.strip() else "none"
    return (
        "<div style='margin:12px 0 4px;font-weight:600'>%s</div>"
        "<pre style='%s;white-space:pre-wrap;margin:0'>%s</pre>"
        % (escape(title), MONO, shown)
    )


def record_html(record: Record, theme: fmt.Theme = fmt.LIGHT) -> str:
    """Render one history entry as the details pane shows it."""
    operation = catalogue.BY_ID.get(record.operation_id)
    rendered = (
        fmt.render(operation, record.status, record.reason, record.response_body)
        if operation is not None
        else None
    )
    colour = theme.ok if record.ok else theme.bad

    parts = [
        "<div style='font-family:sans-serif;font-size:13px;color:%s'>" % theme.ink,
        "<div style='color:%s;font-weight:600;font-size:14px'>%s %s</div>"
        % (colour, escape(record.method), escape(record.path)),
        "<div style='color:%s;margin-bottom:6px'>%s</div>"
        % (theme.muted, escape(record.summary)),
        "<table style='border-collapse:collapse'>",
    ]
    for label, value in (
        ("url", record.url),
        ("started", record.started_at.strftime("%Y-%m-%d %H:%M:%S")),
        ("status", record.outcome + ((" " + record.reason) if record.reason else "")),
        ("took", "%d ms" % record.duration_ms),
    ):
        parts.append(
            "<tr><td style='padding:2px 14px 2px 0;color:%s;white-space:nowrap'>%s</td>"
            "<td style='padding:2px 0'>%s</td></tr>"
            % (theme.muted, escape(label), escape(value))
        )
    parts.append("</table>")

    parts.append(_headers_html("Request headers", record.request_headers, theme))
    parts.append(_body_html("Request body", record.request_body))
    parts.append(_headers_html("Response headers", record.response_headers, theme))

    if rendered is not None:
        parts.append("<div style='margin:12px 0 4px;font-weight:600'>Response, read back</div>")
        parts.append(fmt.as_html(rendered, theme))

    parts.append(_body_html("Response body, exactly as it arrived", record.response_body))
    parts.append("</div>")
    return "".join(parts)


class HistoryDialog(QDialog):
    """Every call this run has made, with a details pane beside the list."""

    def __init__(
        self,
        history: History,
        *,
        parent: QWidget | None = None,
        theme: fmt.Theme = fmt.LIGHT,
    ) -> None:
        """Build the dialog over the run's history."""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._theme = theme
        self.setWindowTitle("Calls made this run")
        self.resize(1000, 620)
        self._records = history.latest_first()

        layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._list = QTableWidget(len(self._records), 4)
        self._list.setHorizontalHeaderLabels(["time", "method", "path", "status"])
        self._list.verticalHeader().setVisible(False)
        self._list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, record in enumerate(self._records):
            self._list.setItem(row, 0, QTableWidgetItem(record.stamp))
            self._list.setItem(row, 1, QTableWidgetItem(record.method))
            self._list.setItem(row, 2, QTableWidgetItem(record.path))
            status = QTableWidgetItem(record.outcome)
            if not record.ok:
                status.setForeground(Qt.GlobalColor.red)
            self._list.setItem(row, 3, status)
        self._list.currentCellChanged.connect(lambda *_args: self._show())
        splitter.addWidget(self._list)

        self._details = QTextBrowser()
        if not self._records:
            self._details.setPlainText("No calls have been made yet.")
        splitter.addWidget(self._details)
        splitter.setSizes([380, 620])
        layout.addWidget(splitter, 1)

        buttons = QDialogButtonBox()
        curl = buttons.addButton("Copy as curl", QDialogButtonBox.ButtonRole.ActionRole)
        curl.clicked.connect(self._copy_curl)
        self._curl_note = QLabel("")
        self._curl_note.setStyleSheet("color:%s" % theme.muted)

        footer = QHBoxLayout()
        footer.addWidget(self._curl_note, 1)
        footer.addWidget(buttons)
        holder = QWidget()
        holder.setLayout(footer)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(holder)

        if self._records:
            self._list.selectRow(0)
            self._show()

    def _current(self) -> Record | None:
        """Return the highlighted record, if there is one."""
        row = self._list.currentRow()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def _show(self) -> None:
        """Fill the details pane from the highlighted record."""
        record = self._current()
        if record is not None:
            self._details.setHtml(record_html(record, self._theme))

    def _copy_curl(self) -> None:
        """Put the highlighted call on the clipboard as a curl command."""
        record = self._current()
        if record is None:
            return
        QGuiApplication.clipboard().setText(record.as_curl())
        self._curl_note.setText(
            "Copied. The API key is a $%s reference, not the key itself." % API_KEY_VARIABLE
        )


class CurlPreview(QDialog):
    """Shows the curl command that has just been put on the clipboard.

    The Copy button is for putting it back after copying something else, not for
    the first copy: by the time this opens, the caller has already done that.
    """

    def __init__(self, command: str, *, parent: QWidget | None = None) -> None:
        """Build the preview around a rendered command."""
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("The same call as curl")
        self.resize(720, 320)

        layout = QVBoxLayout(self)
        body = QTextBrowser()
        body.setStyleSheet(MONO)
        body.setPlainText(command)
        layout.addWidget(body, 1)

        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(command))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)

        row = QHBoxLayout()
        row.addWidget(copy)
        row.addStretch(1)
        row.addWidget(buttons)
        layout.addLayout(row)
