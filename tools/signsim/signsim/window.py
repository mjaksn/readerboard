"""The window: a log of what arrived, and a view of what the sign now holds.

Three things are on screen at once, because in practice all three are wanted
together. The log says what was sent. The detail pane underneath it says what
those bytes mean, one span at a time, in the protocol's own words. The tabs on
the right say what the sign is holding as a result, which is the part no amount
of staring at a packet will tell you.

The colouring is the small thing that makes the rest readable. Framing bytes,
the command and its file label, control sequences, literal text, glyphs and
bytes in no table at all each get their own colour, so the shape of a
transmission is visible before a word of it is read.

Everything shown here comes from the pure modules beside this one. This file
draws; it does not decode.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from PySide6.QtCore import QDateTime, Qt, Slot
from PySide6.QtGui import QAction, QCloseEvent, QColor, QFontDatabase, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from readerboard.protocol import constants as c
from signsim import names
from signsim.decode import (
    DAY_NAMES,
    FILE_TYPE_NAMES,
    RUN_SEQUENCE_MODES,
    DecodedTransmission,
    mode_name,
    position_name,
)
from signsim.model import Note, NoteLevel, SignState
from signsim.server import SignEndpoint
from signsim.spans import Span, SpanKind, annotate, readable

# Enough history to cover an afternoon of debugging without letting a window
# left open over a weekend eat the machine.
MAX_LOG_ROWS = 5000

# Number, time, level, command, summary.
_LOG_COLUMNS = 5

_LIGHT_SPANS = {
    SpanKind.FRAMING: "#6b7280",
    SpanKind.COMMAND: "#1d4ed8",
    SpanKind.CONTROL: "#b45309",
    SpanKind.TEXT: "#047857",
    SpanKind.GLYPH: "#7c3aed",
    SpanKind.UNKNOWN: "#b91c1c",
}

_DARK_SPANS = {
    SpanKind.FRAMING: "#9ca3af",
    SpanKind.COMMAND: "#93c5fd",
    SpanKind.CONTROL: "#fbbf24",
    SpanKind.TEXT: "#6ee7b7",
    SpanKind.GLYPH: "#c4b5fd",
    SpanKind.UNKNOWN: "#fca5a5",
}

_LIGHT_NOTES = {
    NoteLevel.INFO: "#374151",
    NoteLevel.WARNING: "#b45309",
    NoteLevel.VIOLATION: "#b91c1c",
}

_DARK_NOTES = {
    NoteLevel.INFO: "#d1d5db",
    NoteLevel.WARNING: "#fbbf24",
    NoteLevel.VIOLATION: "#fca5a5",
}


@dataclass
class LogEntry:
    """One row of the log, and everything the panes below it need."""

    number: int
    when: str
    decoded: DecodedTransmission
    notes: list[Note] = field(default_factory=list)


class MainWindow(QMainWindow):
    """The whole application window."""

    def __init__(self, endpoint: SignEndpoint, state: SignState) -> None:
        """Wire the window to a listening endpoint and the state it feeds."""
        super().__init__()
        self._endpoint = endpoint
        self._state = state
        self._entries: list[LogEntry] = []
        self._received = 0
        self._pending = 0
        self._last_event = "nothing yet"

        dark = _is_dark(self.palette())
        self._span_colours = _DARK_SPANS if dark else _LIGHT_SPANS
        self._note_colours = _DARK_NOTES if dark else _LIGHT_NOTES
        self._mono = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)

        self.setWindowTitle(names.DISPLAY_NAME)
        self.resize(1400, 860)
        self._build()

        endpoint.transmission.connect(self.on_transmission)
        endpoint.client_connected.connect(self.on_client_connected)
        endpoint.client_disconnected.connect(self.on_client_disconnected)
        endpoint.pending_changed.connect(self.on_pending_changed)

        self._refresh_state()
        self._update_status()

    # == building ==========================================================

    def _build(self) -> None:
        """Lay the window out."""
        self._build_toolbar()

        outer = QSplitter(Qt.Orientation.Horizontal, self)
        outer.addWidget(self._build_log_side())
        outer.addWidget(self._build_state_side())
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 2)
        self.setCentralWidget(outer)

        self._where = QLabel()
        self._client = QLabel()
        self._counts = QLabel()
        for widget in (self._where, self._client, self._counts):
            self.statusBar().addWidget(widget)
            widget.setContentsMargins(0, 0, 16, 0)

    def _build_toolbar(self) -> None:
        """Add the four things worth a button."""
        bar = self.addToolBar("Controls")
        bar.setMovable(False)

        self._pause = QAction("Pause capture", self)
        self._pause.setCheckable(True)
        self._pause.setToolTip(
            "Stop decoding, so the log holds still while a row is being read. "
            "Bytes are still taken off the socket and discarded, so the service "
            "does not stall behind a full buffer."
        )
        self._pause.toggled.connect(self.on_pause_toggled)
        bar.addAction(self._pause)

        clear = QAction("Clear log", self)
        clear.setToolTip("Empty the log. The sign's state is left alone.")
        clear.triggered.connect(self.on_clear_log)
        bar.addAction(clear)

        reset = QAction("Reset sign", self)
        reset.setToolTip(
            "Forget the file table, the messages and the run sequence, as if the "
            "sign had been power cycled with the link still up. This is the case "
            "the service's refresh timer exists to repair."
        )
        reset.triggered.connect(self.on_reset_sign)
        bar.addAction(reset)

        save = QAction("Save transcript", self)
        save.setToolTip("Write the log, with its readings and notes, to a text file.")
        save.triggered.connect(self.on_save_transcript)
        bar.addAction(save)

    def _build_log_side(self) -> QWidget:
        """Build the transmission list, with the detail pane under it."""
        self._log = QTreeWidget()
        self._log.setHeaderLabels(["#", "Time", "Level", "Command", "What it says"])
        self._log.setRootIsDecorated(False)
        self._log.setUniformRowHeights(True)
        self._log.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._log.setAlternatingRowColors(True)
        self._log.currentItemChanged.connect(self.on_row_selected)
        header = self._log.header()
        for column in range(_LOG_COLUMNS - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(_LOG_COLUMNS - 1, QHeaderView.ResizeMode.Stretch)

        # The widget font is left alone deliberately. Setting it to the fixed
        # width face wins over the inline sans-serif on the prose blocks, and
        # the blocks that need alignment name the fixed width family themselves.
        self._detail = QTextBrowser()
        self._detail.setHtml(self._empty_detail())

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self._log)
        split.addWidget(self._detail)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        return split

    def _build_state_side(self) -> QWidget:
        """Build the tabs describing what the sign is holding."""
        self._tabs = QTabWidget()

        self._sign_view = QTextBrowser()
        self._tabs.addTab(self._sign_view, "Sign")

        self._files = _table(["File", "Bytes", "Size", "Mode", "Position", "Message"])
        self._tabs.addTab(self._files, "Files")

        self._memory = _table(["File", "Type", "Size", "Keyboard", "Schedule"])
        self._tabs.addTab(self._memory, "Memory")

        self._sequence = _table(["Order", "File", "Allocated", "Holds a message"])
        self._tabs.addTab(self._sequence, "Run sequence")

        self._notes = QTextBrowser()
        self._tabs.addTab(self._notes, "Notes")

        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._tabs)
        return holder

    # == events ============================================================

    @Slot(object)
    def on_transmission(self, decoded: DecodedTransmission) -> None:
        """Take one decoded transmission: log it, apply it, redraw."""
        self._received += 1
        notes = self._state.apply(decoded.command)
        entry = LogEntry(
            number=self._received,
            when=QDateTime.currentDateTime().toString("HH:mm:ss.zzz"),
            decoded=decoded,
            notes=notes,
        )
        self._entries.append(entry)

        worst = _worst_level(entry)
        # The level is spelled out as well as coloured. Colour alone puts the
        # one thing worth spotting out of reach of anybody who cannot separate
        # amber from grey, and out of reach of a screen reader entirely.
        item = QTreeWidgetItem(
            [
                str(entry.number),
                entry.when,
                "" if worst is None else worst.value,
                decoded.command.name,
                decoded.command.summary,
            ]
        )
        spoken = "%s. %s" % (
            "no problems reported" if worst is None else worst.value,
            decoded.command.summary,
        )
        for column in range(_LOG_COLUMNS):
            item.setToolTip(column, spoken)
            item.setData(column, Qt.ItemDataRole.AccessibleDescriptionRole, spoken)
        if worst is not None:
            colour = QColor(self._note_colours[worst])
            for column in range(_LOG_COLUMNS):
                item.setForeground(column, colour)
        self._log.addTopLevelItem(item)

        while self._log.topLevelItemCount() > MAX_LOG_ROWS:
            self._log.takeTopLevelItem(0)
            self._entries.pop(0)

        # The newest transmission is always selected, because the usual reason
        # this window is open is to see what just happened. Studying an older
        # row while traffic continues is what the pause button is for; its
        # tooltip says so.
        self._log.setCurrentItem(item)
        self._log.scrollToItem(item)

        self._refresh_state()
        self._update_status()

    @Slot(QTreeWidgetItem, QTreeWidgetItem)
    def on_row_selected(self, current: QTreeWidgetItem | None, _previous: object) -> None:
        """Render the selected transmission into the detail pane."""
        if current is None:
            self._detail.setHtml(self._empty_detail())
            return
        index = self._log.indexOfTopLevelItem(current)
        if 0 <= index < len(self._entries):
            self._detail.setHtml(self._render_detail(self._entries[index]))

    @Slot(bool)
    def on_pause_toggled(self, paused: bool) -> None:
        """Stop or resume decoding."""
        self._endpoint.set_paused(paused)
        self._pause.setText("Resume capture" if paused else "Pause capture")
        self._update_status()

    @Slot()
    def on_clear_log(self) -> None:
        """Empty the log, leaving the sign's state as it is."""
        self._entries.clear()
        self._log.clear()
        self._detail.setHtml(self._empty_detail())
        # The notes tab is drawn from the entries, so it has to be redrawn here
        # or it keeps listing notes against transmissions that are gone. Nothing
        # else would redraw it until the next one arrives, and traffic having
        # stopped is the usual reason for clearing in the first place.
        self._refresh_notes()
        self._update_status()

    @Slot()
    def on_reset_sign(self) -> None:
        """Forget everything the sign holds, as a power cycle would."""
        self._state.reset()
        self._refresh_state()
        self._update_status()

    @Slot()
    def on_save_transcript(self) -> None:
        """Write the log out as plain text."""
        path, _filter = QFileDialog.getSaveFileName(
            self, "Save transcript", "signsim-transcript.txt", "Text files (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._transcript())
        except OSError as err:
            QMessageBox.warning(self, "Could not save", "%s" % err)

    @Slot(str)
    def on_client_connected(self, who: str) -> None:
        """Note a client attaching."""
        self._update_status(last="%s connected" % who)

    @Slot(str)
    def on_client_disconnected(self, who: str) -> None:
        """Note a client detaching."""
        self._update_status(last="%s disconnected" % who)

    @Slot(int)
    def on_pending_changed(self, pending: int) -> None:
        """Show how much of a transmission is buffered but not yet complete."""
        self._pending = pending
        self._update_status()

    # == rendering =========================================================

    def _empty_detail(self) -> str:
        """Say what the detail pane shows before anything has arrived."""
        return (
            "<p style='font-family:sans-serif'>Waiting for a transmission. Point the "
            "service at <code>%s</code> and it will appear here.</p>%s"
            % (html.escape(self._endpoint.serial_url), self._legend())
        )

    def _legend(self) -> str:
        """Render the key to the colours, which is worth having on screen."""
        cells = "".join(
            "<span style='color:%s'>&#9632; %s</span>&nbsp;&nbsp;"
            % (self._span_colours[kind], kind.value)
            for kind in SpanKind
        )
        return "<p style='font-family:sans-serif;font-size:small'>%s</p>" % cells

    def _render_detail(self, entry: LogEntry) -> str:
        """Render the full reading of one transmission."""
        decoded = entry.decoded
        transmission = decoded.transmission
        parts = [
            "<div style='font-family:sans-serif'>",
            "<h3 style='margin-bottom:2px'>%s</h3>" % html.escape(decoded.command.name),
            "<p style='margin-top:0'>%s</p>" % html.escape(decoded.command.summary),
            "</div>",
            self._legend(),
            "<p style='font-family:sans-serif;font-size:small;margin-bottom:2px'>"
            "<b>On the wire</b>: %d wakeup null(s), then %d byte(s):</p>"
            % (transmission.wakeup_nulls, len(transmission.raw)),
            self._hex_block(decoded.spans),
            self._span_table(decoded.spans),
        ]

        if decoded.command.details:
            rows = "".join(
                "<tr><td style='padding-right:12px'><b>%s</b></td><td>%s</td>"
                "<td style='padding-left:12px;opacity:0.7'>%s</td></tr>"
                % (html.escape(one.name), html.escape(one.value), html.escape(one.note))
                for one in decoded.command.details
            )
            parts.append(
                "<p style='font-family:sans-serif;font-size:small;margin-bottom:2px'>"
                "<b>Reading</b></p><table style='font-family:sans-serif;font-size:small'>"
                "%s</table>" % rows
            )

        if transmission.junk_before_count:
            parts.append(
                "<p style='font-family:sans-serif;font-size:small'><b>Preceded by</b> "
                "%d byte(s) that were neither wakeup nulls nor a header: <code>%s</code></p>"
                % (transmission.junk_before_count, html.escape(_hex(transmission.junk_before)))
            )

        parts.append(self._complaints_block(decoded.complaints))
        parts.append(self._notes_block(entry.notes))
        return "".join(parts)

    def _hex_block(self, spans: tuple[Span, ...]) -> str:
        """Render the whole frame as coloured hex."""
        chunks = [
            "<span style='color:%s'>%s</span>"
            % (self._span_colours[span.kind], _hex(span.data))
            for span in spans
        ]
        return (
            "<div style='font-family:%s;font-size:small;line-height:1.6'>%s</div>"
            % (self._mono.family(), " ".join(chunks))
        )

    def _span_table(self, spans: tuple[Span, ...]) -> str:
        """Render every span, with the protocol's meaning beside it.

        The hex column is capped and the columns are given widths that add up
        to the pane. Without both, one long run of literal text makes its own
        cell wider than the pane and the description beside it wraps to a
        single character per line, which is unreadable exactly when the message
        is interesting. The whole frame is above this in full anyway.
        """
        rows = []
        for span in spans:
            rows.append(
                "<tr>"
                "<td width='5%%' style='padding-right:10px;opacity:0.6'>%d</td>"
                "<td width='23%%' style='padding-right:10px;color:%s'>%s</td>"
                "<td width='20%%' style='padding-right:10px;color:%s'><b>%s</b></td>"
                "<td width='52%%'>%s</td>"
                "</tr>"
                % (
                    span.offset,
                    self._span_colours[span.kind],
                    html.escape(_hex_capped(span.data)),
                    self._span_colours[span.kind],
                    html.escape(span.label),
                    html.escape(span.description),
                )
            )
        return (
            "<p style='font-family:sans-serif;font-size:small;margin-bottom:2px'>"
            "<b>Byte by byte</b></p>"
            "<table width='100%%' style='font-family:%s;font-size:small'>%s</table>"
            % (self._mono.family(), "".join(rows))
        )

    def _complaints_block(self, complaints: tuple[str, ...]) -> str:
        """Render the things the sign would have shrugged off without saying so."""
        if not complaints:
            return ""
        items = "".join(
            "<li style='color:%s'>%s</li>"
            % (self._note_colours[NoteLevel.WARNING], html.escape(one))
            for one in complaints
        )
        return (
            "<p style='font-family:sans-serif;font-size:small;margin-bottom:2px'>"
            "<b>Problems with this transmission</b></p>"
            "<ul style='font-family:sans-serif;font-size:small'>%s</ul>" % items
        )

    def _notes_block(self, notes: list[Note]) -> str:
        """Render what the sign did about it."""
        if not notes:
            return ""
        items = "".join(
            "<li style='color:%s'><b>%s</b> %s</li>"
            % (self._note_colours[note.level], note.level.value, html.escape(note.text))
            for note in notes
        )
        return (
            "<p style='font-family:sans-serif;font-size:small;margin-bottom:2px'>"
            "<b>What the sign did</b></p>"
            "<ul style='font-family:sans-serif;font-size:small'>%s</ul>" % items
        )

    # == the state tabs ====================================================

    def _refresh_state(self) -> None:
        """Redraw every panel from the sign state."""
        self._refresh_sign_view()
        self._refresh_files()
        self._refresh_memory()
        self._refresh_sequence()
        self._refresh_notes()

    def _refresh_sign_view(self) -> None:
        """Summarise what the sign would be doing right now."""
        state = self._state
        rows: list[tuple[str, str]] = []

        if state.priority_active:
            showing = "the priority file, which suppresses everything else"
        elif state.playing:
            showing = "cycling %s by itself, with no traffic per rotation" % ", ".join(
                label.decode("latin-1") for label in state.playing
            )
        else:
            showing = "nothing"
        rows.append(("Showing", showing))

        if state.priority_active:
            rows.append(("Priority message", _rendered(state.priority)))

        if state.memory_config is None:
            rows.append(
                (
                    "Memory",
                    "not configured. Only the priority file and the default file A "
                    "can be written until it is",
                )
            )
        else:
            rows.append(
                (
                    "Memory",
                    "%d file(s), %d bytes claimed including %d bytes of overhead each"
                    % (
                        len(state.memory_config),
                        state.memory_claimed,
                        c.FILE_OVERHEAD_BYTES,
                    ),
                )
            )

        if state.run_sequence_mode:
            rows.append(
                (
                    "Run sequence order",
                    "%s%s"
                    % (
                        RUN_SEQUENCE_MODES.get(
                            state.run_sequence_mode, "an order this tool does not recognise"
                        ),
                        ", locked against the infrared keyboard"
                        if state.run_sequence_locked
                        else "",
                    ),
                )
            )

        clock = "not set"
        if state.hour is not None and state.minute is not None:
            clock = "%02d:%02d" % (state.hour, state.minute)
        if state.day:
            clock += " on %s" % DAY_NAMES[state.day - 1]
        if state.military_time is not None:
            clock += ", shown on a %s hour clock" % ("24" if state.military_time else "12")
        rows.append(("Clock", clock))

        rows.append(("Transmissions applied", str(state.transmissions)))

        body = "".join(
            "<tr><td style='padding-right:16px;vertical-align:top'><b>%s</b></td>"
            "<td>%s</td></tr>" % (html.escape(name), html.escape(value))
            for name, value in rows
        )
        self._sign_view.setHtml(
            "<table style='font-family:sans-serif;font-size:small'>%s</table>" % body
        )

    def _refresh_files(self) -> None:
        """One row per TEXT file the sign is holding."""
        state = self._state
        labels = sorted(state.files)
        self._files.setRowCount(len(labels) + (1 if state.priority_active else 0))

        row = 0
        if state.priority_active:
            _fill(
                self._files,
                row,
                [
                    "0 (priority)",
                    str(len(state.priority)),
                    str(c.PRIORITY_FILE_CAPACITY),
                    "",
                    "",
                    _rendered(state.priority),
                ],
            )
            row += 1

        for label in labels:
            stored = state.files[label]
            capacity = state.capacity_of(label)
            sent = len(stored.body)
            size = (
                str(sent)
                if stored.truncated_to is None
                else "%d sent, %d kept" % (sent, stored.truncated_to)
            )
            _fill(
                self._files,
                row,
                [
                    label.decode("latin-1"),
                    size,
                    "?" if capacity is None else str(capacity),
                    mode_name(stored.mode) if stored.mode else "",
                    position_name(stored.position) if stored.position else "",
                    stored.rendered,
                ],
            )
            row += 1

    def _refresh_memory(self) -> None:
        """Show the file table as the sign holds it."""
        config = self._state.memory_config
        order = self._state.memory_order
        self._memory.setRowCount(0 if config is None else len(order))
        if config is None:
            return
        for row, label in enumerate(order):
            entry = config[label]
            _fill(
                self._memory,
                row,
                [
                    label.decode("latin-1"),
                    FILE_TYPE_NAMES.get(entry.file_type, "?"),
                    "%d%s" % (entry.capacity, " (plus the rest of the pool)" if row == 0 else ""),
                    "locked" if entry.locked else "editable",
                    "always" if entry.always_eligible else entry.schedule.decode("latin-1"),
                ],
            )

    def _refresh_sequence(self) -> None:
        """Show the run sequence, and whether each label in it is worth anything."""
        state = self._state
        self._sequence.setRowCount(len(state.run_sequence))
        for row, label in enumerate(state.run_sequence):
            allocated = state.capacity_of(label) is not None
            stored = state.files.get(label)
            _fill(
                self._sequence,
                row,
                [
                    str(row + 1),
                    label.decode("latin-1"),
                    "yes" if allocated else "no, so the sign skips it",
                    "yes" if stored and stored.body else "no",
                ],
            )

    def _refresh_notes(self) -> None:
        """Every note so far, newest last."""
        rows = []
        for entry in self._entries:
            for note in entry.notes:
                rows.append(
                    "<li style='color:%s'><b>#%d %s</b> %s</li>"
                    % (
                        self._note_colours[note.level],
                        entry.number,
                        note.level.value,
                        html.escape(note.text),
                    )
                )
        if not rows:
            self._notes.setHtml(
                "<p style='font-family:sans-serif;font-size:small'>Nothing to report yet. "
                "This tab collects everything the sign would have done quietly: writes it "
                "discards, messages it truncates, and files it is asked to play but does "
                "not have.</p>"
            )
            return
        self._notes.setHtml(
            "<ul style='font-family:sans-serif;font-size:small'>%s</ul>" % "".join(rows)
        )
        bar = self._notes.verticalScrollBar()
        bar.setValue(bar.maximum())

    # == status bar ========================================================

    def _update_status(self, last: str | None = None) -> None:
        """Refresh the three status bar labels."""
        if last is not None:
            self._last_event = last
        self._where.setText("Listening on %s" % self._endpoint.serial_url)
        self._client.setText(
            "%d client(s), last: %s" % (self._endpoint.client_count, self._last_event)
        )
        parts = ["%d transmission(s)" % self._received]
        if self._pending:
            parts.append("%d byte(s) of a transmission still buffered" % self._pending)
        if self._pause.isChecked():
            parts.append("capture paused")
        self._counts.setText("  |  ".join(parts))

    def _transcript(self) -> str:
        """Render the whole log as plain text, for pasting into an issue."""
        lines = [
            "readerboard sign simulator transcript",
            "listening on %s" % self._endpoint.serial_url,
            "",
        ]
        for entry in self._entries:
            decoded = entry.decoded
            lines.append(
                "#%d  %s  %s" % (entry.number, entry.when, decoded.command.name)
            )
            lines.append("    %s" % decoded.command.summary)
            lines.append("    raw: %s" % _hex(decoded.transmission.raw))
            for span in decoded.spans:
                lines.append(
                    "      %4d  %-24s %-26s %s"
                    % (span.offset, _hex(span.data), span.label, span.description)
                )
            for complaint in decoded.complaints:
                lines.append("    problem: %s" % complaint)
            for note in entry.notes:
                lines.append("    %s: %s" % (note.level.value, note.text))
            lines.append("")
        return "\n".join(lines)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop listening on the way out."""
        self._endpoint.close()
        super().closeEvent(event)


# ===========================================================================
# Small helpers.
# ===========================================================================


def _table(headers: list[str]) -> QTableWidget:
    """Make a read-only table set up the way every panel here wants one."""
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    header = table.horizontalHeader()
    for column in range(len(headers) - 1):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    return table


def _fill(table: QTableWidget, row: int, values: list[str]) -> None:
    """Put one row of strings into a table.

    Each cell carries its own text as a tooltip, because the message column is
    the interesting one and it is also the one a narrow window elides.
    """
    for column, value in enumerate(values):
        cell = QTableWidgetItem(value)
        cell.setToolTip(value)
        table.setItem(row, column, cell)


def _rendered(body: bytes) -> str:
    """Read a stored message back as the markup that produced it.

    The priority file goes through this for the same reason every other file
    does. An alert is rendered by the service exactly as a message is, so
    showing its raw bytes puts control codes into a cell that draws them as
    nothing: an alert of ``<red><flash_on>FIRE`` would appear as bare FIRE with
    invisible noise around it, which is the case somebody opened this to see.
    """
    return readable(annotate(body))


def _hex(data: bytes) -> str:
    """Render bytes as space separated uppercase hex."""
    return " ".join("%02X" % byte for byte in data)


def _hex_capped(data: bytes, limit: int = 4) -> str:
    """Render bytes as hex, shortened, for a column that has to stay narrow.

    Four is low enough that the cell never wraps at the width the table gives
    it. Nothing is lost: the whole frame is above this in full, and a span long
    enough to be cut here is a run of literal text whose description spells it
    out anyway.
    """
    if len(data) <= limit:
        return _hex(data)
    return "%s ... %d bytes" % (_hex(data[:limit]), len(data))


def _worst_level(entry: LogEntry) -> NoteLevel | None:
    """Return the most serious thing about a transmission, to colour its row."""
    if any(note.level is NoteLevel.VIOLATION for note in entry.notes):
        return NoteLevel.VIOLATION
    if entry.decoded.complaints or any(
        note.level is NoteLevel.WARNING for note in entry.notes
    ):
        return NoteLevel.WARNING
    return None


def _is_dark(palette: QPalette) -> bool:
    """Whether the window is being drawn on a dark background."""
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


__all__ = ["MainWindow"]
