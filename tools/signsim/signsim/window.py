"""The window: a log of what arrived, and a view of what the sign now holds.

Four things are on screen at once, because in practice all four are wanted
together. The band across the top says what the sign would be showing this
second, which is the one fact worth never having to look for. The log under it,
the full width of the window, says what was sent. The detail pane below left
says what those bytes mean, one span at a time, in the protocol's own words. The
column beside it says what the sign is holding as a result, which is the part no
amount of staring at a packet will tell you.

That last column is a stack of collapsible sections rather than a set of tabs,
and the difference is the point of it. A run sequence write that lands in a tab
nobody has open changes nothing a person can see, which is the opposite of what
this tool is for. The sections that have nothing in them yet stay shut and open
themselves when they first hold a row, so the column grows as the sign is
configured instead of standing there as headings over four empty tables.

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

from PySide6.QtCore import QDateTime, QMargins, QModelIndex, Qt, Slot
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QFontDatabase,
    QPalette,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QToolButton,
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

# How many rows of a table in the state column are given room before it starts
# scrolling inside its own section. A scrolling region inside a scrolling column
# is worth avoiding, and this is the compromise: a full pool of eight files fits
# without one, and anything longer costs a scrollbar rather than the whole
# column's height.
MAX_SECTION_ROWS = 8

# The tallest the Sign section is allowed to be. It is a handful of name and
# value rows whose height depends on how much of each one wraps, so it is fitted
# to its content and capped rather than fixed.
MAX_SIGN_HEIGHT = 220

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

    The client carries the same function, for the reason it carries its own lock
    file: the two tools share nothing, so that one can move without the other.
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


class _Section(QWidget):
    """One titled, collapsible block of the state column.

    The header doubles as the count, so a shut section still says whether it has
    anything in it. That is what makes shutting one safe: a person who collapses
    Files to get at Run sequence can still see that Files gained a row.

    ``opened_itself`` is why the auto-open only ever fires once. A section that
    opens the first time it holds a row is helpful; one that springs open again
    every time a row arrives, after it has been deliberately shut, is not.
    """

    def __init__(self, title: str, content: QWidget, *, expanded: bool) -> None:
        """Wrap a widget in a header that hides and shows it."""
        super().__init__()
        self._title = title
        self._content = content
        self.opened_itself = expanded

        self._header = QToolButton()
        self._header.setCheckable(True)
        self._header.setChecked(expanded)
        self._header.setAutoRaise(True)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setStyleSheet("font-weight:600")
        self._header.setText(title)
        self._header.toggled.connect(self._on_toggled)
        self._set_arrow(expanded)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(2)
        layout.addWidget(self._header)
        layout.addWidget(content)
        content.setVisible(expanded)

    def set_count(self, count: int) -> None:
        """Say how many rows the section holds, and open it the first time it has any."""
        self._header.setText("%s (%d)" % (self._title, count) if count else self._title)
        if count and not self.opened_itself:
            self.opened_itself = True
            self._header.setChecked(True)

    @Slot(bool)
    def _on_toggled(self, open_now: bool) -> None:
        """Show or hide the content, and turn the arrow to match."""
        self._content.setVisible(open_now)
        self._set_arrow(open_now)
        # Collapsing by hand counts as having made a decision about this
        # section, so nothing later opens it again.
        self.opened_itself = True

    def _set_arrow(self, open_now: bool) -> None:
        """Point the arrow down when open and right when shut."""
        self._header.setArrowType(
            Qt.ArrowType.DownArrow if open_now else Qt.ArrowType.RightArrow
        )


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
        fit_to_screen(self)
        self._build()

        endpoint.transmission.connect(self.on_transmission)
        endpoint.client_connected.connect(self.on_client_connected)
        endpoint.client_disconnected.connect(self.on_client_disconnected)
        endpoint.pending_changed.connect(self.on_pending_changed)

        self._refresh_state()
        self._update_status()

    # == building ==========================================================

    def _build(self) -> None:
        """Lay the window out.

        The showing band is fixed above the splitters rather than inside one,
        because it is the only thing here that must never be dragged away.
        Neither splitter lets a child collapse to nothing for the same reason:
        a pane at zero width looks like a bug and has no obvious way back.
        """
        self._build_toolbar()

        lower = QSplitter(Qt.Orientation.Horizontal)
        lower.addWidget(self._build_detail_side())
        lower.addWidget(self._build_state_side())
        lower.setStretchFactor(0, 3)
        lower.setStretchFactor(1, 2)
        lower.setChildrenCollapsible(False)

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(self._build_log())
        outer.addWidget(lower)
        outer.setStretchFactor(0, 2)
        outer.setStretchFactor(1, 3)
        outer.setChildrenCollapsible(False)

        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(6, 6, 6, 0)
        layout.setSpacing(6)
        layout.addWidget(self._build_showing())
        layout.addWidget(outer, 1)
        self.setCentralWidget(holder)

        self._where = QLabel()
        self._client = QLabel()
        self._counts = QLabel()
        for widget in (self._where, self._client, self._counts):
            self.statusBar().addWidget(widget)
            widget.setContentsMargins(0, 0, 16, 0)

    def _build_toolbar(self) -> None:
        """Add the five things worth a button."""
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

        bar.addSeparator()

        self._only_notes = QAction("Only rows with notes", self)
        self._only_notes.setCheckable(True)
        self._only_notes.setToolTip(
            "Hide every transmission the sign had nothing to say about, leaving "
            "the writes it discarded, the messages it truncated and the files it "
            "was asked to play but does not have. Nothing is thrown away: "
            "clearing this shows them again, and a saved transcript holds every "
            "transmission either way."
        )
        self._only_notes.toggled.connect(self.on_only_notes_toggled)
        bar.addAction(self._only_notes)

    def _build_showing(self) -> QWidget:
        """Build the band that says what the sign would be showing right now.

        This is the tool's headline fact and it used to be the first row of a
        panel behind a tab, which meant the window could be open and busy and
        never say what was on the sign.
        """
        self._showing = QLabel()
        self._showing.setWordWrap(True)
        self._showing.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._showing.setToolTip(
            "What the sign would be displaying at this moment: the files it is "
            "cycling, or the priority message suppressing them."
        )
        return self._showing

    def _build_log(self) -> QWidget:
        """Build the transmission list, which spans the window."""
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
        # Roughly six rows. The window is three bands tall now, and on a scaled
        # desktop, where the whole thing is 800 logical pixels, the splitter
        # would otherwise be free to leave the log showing one.
        self._log.setMinimumHeight(140)
        return self._log

    def _build_detail_side(self) -> QWidget:
        """Build the pane that reads the selected transmission byte by byte."""
        # The widget font is left alone deliberately. Setting it to the fixed
        # width face wins over the inline sans-serif on the prose blocks, and
        # the blocks that need alignment name the fixed width family themselves.
        self._detail = QTextBrowser()
        self._detail.setHtml(self._empty_detail())
        return self._detail

    def _build_state_side(self) -> QWidget:
        """Build the column of sections describing what the sign is holding.

        Only Sign starts open. The other three are empty until a command
        arrives, and a heading over an empty table says less than a shut section
        that will open itself the moment it has a row.
        """
        self._sign_view = _FittedBrowser(MAX_SIGN_HEIGHT)
        self._files = _FittedTable(["File", "Bytes", "Size", "Mode", "Position", "Message"])
        self._memory = _FittedTable(["File", "Type", "Size", "Keyboard", "Schedule"])
        self._sequence = _FittedTable(["Order", "File", "Allocated", "Holds a message"])

        self._sign_section = _Section("Sign", self._sign_view, expanded=True)
        self._files_section = _Section("Files", self._files, expanded=False)
        self._memory_section = _Section("Memory", self._memory, expanded=False)
        self._sequence_section = _Section("Run sequence", self._sequence, expanded=False)

        stack = QWidget()
        layout = QVBoxLayout(stack)
        layout.setContentsMargins(4, 0, 4, 4)
        layout.setSpacing(0)
        for section in (
            self._sign_section,
            self._files_section,
            self._memory_section,
            self._sequence_section,
        ):
            layout.addWidget(section)
        layout.addStretch(1)

        column = QScrollArea()
        column.setWidgetResizable(True)
        column.setWidget(stack)
        column.setFrameShape(QScrollArea.Shape.NoFrame)
        # Narrower than this and the Files table's message column has nothing
        # left after the five that size themselves to their contents.
        column.setMinimumWidth(320)
        return column

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
        item.setHidden(self._only_notes.isChecked() and worst is None)

        while self._log.topLevelItemCount() > MAX_LOG_ROWS:
            self._log.takeTopLevelItem(0)
            self._entries.pop(0)

        # The newest transmission is normally selected, because the usual reason
        # this window is open is to see what just happened. Studying an older
        # row while traffic continues is what the pause button is for; its
        # tooltip says so.
        #
        # A row the filter is hiding is the exception. Selecting it would empty
        # the detail pane of the row being read and put a reading of something
        # invisible in its place, which is the opposite of what the filter was
        # turned on for.
        if not item.isHidden():
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

    @Slot(bool)
    def on_only_notes_toggled(self, _only: bool) -> None:
        """Hide or show the transmissions the sign had nothing to say about."""
        self._apply_filter()
        self._update_status()

    @Slot()
    def on_clear_log(self) -> None:
        """Empty the log, leaving the sign's state as it is."""
        self._entries.clear()
        self._log.clear()
        self._detail.setHtml(self._empty_detail())
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

    def _apply_filter(self) -> None:
        """Hide or show every row to match the filter, and keep a visible selection.

        The current row is left alone when it survives the filter, because
        turning the filter on to look at a note should not move a person off the
        row they were reading. When it does not survive, the newest row that
        does takes its place: leaving a hidden row current shows a reading of
        something that is not on screen.
        """
        only = self._only_notes.isChecked()
        last_shown: QTreeWidgetItem | None = None
        for index, entry in enumerate(self._entries):
            item = self._log.topLevelItem(index)
            if item is None:
                continue
            item.setHidden(only and _worst_level(entry) is None)
            if not item.isHidden():
                last_shown = item

        current = self._log.currentItem()
        if current is not None and not current.isHidden():
            return
        if last_shown is None:
            # An invalid index is how the current item is cleared. Leaving a
            # hidden row current would keep its reading in the detail pane with
            # nothing on screen to say which row it belongs to.
            self._log.setCurrentIndex(QModelIndex())
            self._detail.setHtml(self._empty_detail())
            return
        self._log.setCurrentItem(last_shown)
        self._log.scrollToItem(last_shown)

    def _hidden_rows(self) -> int:
        """How many rows the filter is holding back."""
        if not self._only_notes.isChecked():
            return 0
        return sum(1 for entry in self._entries if _worst_level(entry) is None)

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

    # == the state column ==================================================

    def _refresh_state(self) -> None:
        """Redraw every panel from the sign state."""
        self._refresh_showing()
        self._refresh_sign_view()
        self._refresh_files()
        self._refresh_memory()
        self._refresh_sequence()

    def _refresh_showing(self) -> None:
        """Say what the sign would be displaying, in the band across the top.

        An alert is coloured as a violation is, not because anything is wrong
        with it but because it is the state that hides everything else, and the
        band is the only thing on screen that says so.
        """
        state = self._state
        if state.priority_active:
            colour = self._note_colours[NoteLevel.VIOLATION]
            text = "Showing the priority message, which suppresses every other file: %s" % (
                _rendered(state.priority)
            )
        elif state.playing:
            colour = self._note_colours[NoteLevel.INFO]
            text = "Showing %s, cycled by the sign itself with no traffic per rotation" % (
                ", ".join(label.decode("latin-1") for label in state.playing)
            )
        else:
            colour = self._note_colours[NoteLevel.INFO]
            # Two different reasons, and saying which is the whole value of the
            # line. ``playing`` skips a label that is not an allocated TEXT
            # file, so an empty list means either that nothing was asked for or
            # that everything asked for was skipped.
            text = "Showing nothing. %s" % (
                "No run sequence has been set."
                if not state.run_sequence
                else "The sign skips every file the run sequence names, because "
                "none of them is an allocated TEXT file."
            )
        self._showing.setStyleSheet(
            "padding:6px 9px;border-radius:4px;font-weight:600;color:%s" % colour
        )
        self._showing.setText(text)

    def _refresh_sign_view(self) -> None:
        """Summarise what the sign is set up to do.

        What it is showing this second is not here. That went to the band across
        the top of the window, where it is read without opening anything.
        """
        state = self._state
        rows: list[tuple[str, str]] = []

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

        self._files_section.set_count(self._files.rowCount())
        self._files.fit()

    def _refresh_memory(self) -> None:
        """Show the file table as the sign holds it."""
        config = self._state.memory_config
        order = self._state.memory_order
        self._memory.setRowCount(0 if config is None else len(order))
        self._memory_section.set_count(self._memory.rowCount())
        if config is None:
            self._memory.fit()
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
        self._memory.fit()

    def _refresh_sequence(self) -> None:
        """Show the run sequence, and whether each label in it is worth anything."""
        state = self._state
        self._sequence.setRowCount(len(state.run_sequence))
        self._sequence_section.set_count(self._sequence.rowCount())
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
        self._sequence.fit()

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
        # Said here rather than left to the toolbar's pressed button, because a
        # filtered log and a quiet one look identical and only one of them means
        # the service has stopped writing.
        hidden = self._hidden_rows()
        if hidden:
            parts.append("%d row(s) hidden by the filter" % hidden)
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


class _FittedTable(QTableWidget):
    """A read-only table exactly as tall as its rows, up to a limit.

    A table left to its own devices in a scrolling column asks for a size that
    has nothing to do with what is in it, and scrolls inside itself while the
    column scrolls around it. Sizing each one to its contents is what keeps the
    column a single list rather than a set of little windows onto other lists.

    The horizontal scrollbar is the awkward part. Most of these columns size
    themselves to their contents, so in a column this narrow one often appears,
    and a height that does not allow for it hides the last row behind it. Which
    way it will go cannot be worked out in advance: the width the table will be
    given is not known while the section holding it is still being opened, and
    guessing from the column widths is wrong in both directions.

    So it does not guess. It asks whether the scrollbar is there, and re-fits on
    every resize, which is what the scrollbar appearing or disappearing causes.
    The first height may be off by the depth of a scrollbar; the one after it,
    which is the one anybody sees, is right.
    """

    def __init__(self, headers: list[str], limit: int = MAX_SECTION_ROWS) -> None:
        """Build the table set up the way every section here wants one."""
        super().__init__(0, len(headers))
        self._limit = limit
        self.setHorizontalHeaderLabels(headers)
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        header = self.horizontalHeader()
        for column in range(len(headers) - 1):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-fit, because a scrollbar coming or going changes the height needed."""
        super().resizeEvent(event)
        self.fit()

    def fit(self) -> None:
        """Take the height these rows need. Call it after filling them, not before."""
        # One row's worth when empty, so a section opened on an empty table
        # shows its headings rather than a sliver.
        rows = min(self.rowCount(), self._limit) or 1
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if self.rowCount() > self._limit
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        gutter = (
            self.horizontalScrollBar().sizeHint().height()
            if self.horizontalScrollBar().isVisible()
            else 0
        )
        wanted = (
            self.horizontalHeader().height()
            + rows * self.verticalHeader().defaultSectionSize()
            + gutter
            + 2 * self.frameWidth()
            + 2
        )
        if wanted != self.height():
            self.setFixedHeight(wanted)


class _FittedBrowser(QTextBrowser):
    """A text pane exactly as tall as its document, up to a limit.

    Fitting on every resize as well as on every ``setHtml`` is what makes this
    right rather than nearly right. The height a document needs depends on the
    width it is given, and the width is not known until the column has been laid
    out, so a height worked out once when the widget was built is a height for a
    width the widget never had.
    """

    def __init__(self, limit: int) -> None:
        """Build a pane that will keep itself no taller than ``limit``."""
        super().__init__()
        self._limit = limit
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    def setHtml(self, text: str) -> None:
        """Set the content, then take the height it turned out to need."""
        super().setHtml(text)
        self.fit()

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Re-fit, because a new width means a new height."""
        super().resizeEvent(event)
        self.fit()

    def fit(self) -> None:
        """Take the height this document needs at the current width."""
        document = self.document()
        document.setTextWidth(self.viewport().width())
        height = int(document.size().height()) + 2 * self.frameWidth() + 4
        wanted = min(height, self._limit)
        if wanted != self.height():
            self.setFixedHeight(wanted)


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
