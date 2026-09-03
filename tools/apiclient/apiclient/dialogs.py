"""The windows that open on top: enumerations, errors, the call history and the curl preview.

Everything here exists so the main window can stay one screen. A set of markup
tokens, the full text of an error, a network tab's worth of detail and the curl
command for the call just made are all things worth having and none of them are
worth a permanent quarter of the window, so each gets a dialog and the screen
keeps its shape.
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
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
from apiclient.enums import Entry
from apiclient.history import History, Record
from apiclient.request import API_KEY_VARIABLE

MONO = "font-family:Consolas,'DejaVu Sans Mono',monospace;font-size:12px"

# The two things a failed call can mean, kept apart. A refused connection, an
# address that will not resolve and a transfer that timed out are the one kind
# of failure where no service reported anything, because none was reached.
SERVICE_PROBLEM = "The service reported a problem"
NOTHING_ANSWERED = "Nothing answered"


class EntryPicker(QDialog):
    """Shows one enumeration, and optionally hands a name back to be inserted.

    The same dialog does both jobs. Opened from the enumerations panel it is a
    reference; opened from the button beside a message field it is a picker, and
    double clicking inserts.
    """

    def __init__(
        self,
        title: str,
        entries: tuple[Entry, ...],
        *,
        parent: QWidget | None = None,
        insert: bool = False,
    ) -> None:
        """Build the dialog for a set of entries."""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(620, 520)
        self.selected: str | None = None
        self._entries = entries

        layout = QVBoxLayout(self)

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("filter by name or description")
        self._filter.setClearButtonEnabled(True)
        self._filter.textChanged.connect(self._apply_filter)
        layout.addWidget(self._filter)

        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["name", "description"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self._table.itemDoubleClicked.connect(lambda _item: self._take())
        layout.addWidget(self._table, 1)

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
        for row, entry in enumerate(matching):
            self._table.setItem(row, 0, QTableWidgetItem(entry.name))
            self._table.setItem(row, 1, QTableWidgetItem(entry.description))
        self._table.resizeColumnToContents(0)
        if matching:
            self._table.selectRow(0)

    def _take(self) -> None:
        """Accept the highlighted row as the choice."""
        item = self._table.item(self._table.currentRow(), 0)
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
