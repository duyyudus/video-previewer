"""Responsive thumbnail grid (QListView + model/view + custom delegate).

Pointer handling (via an event filter on the viewport) maps each tile to:
  * enter  -> start hover preview (muted autoplay after a short delay)
  * move   -> within the timeline strip at the tile's bottom edge, the
    horizontal position scrubs the shared player (throttled)
  * leave  -> stop the player, restore the static thumbnail

A double-click opens the tile's video with the OS-default player; on empty
grid space (no video under the pointer) it emits ``open_folder_requested``
so the window can offer its folder picker. Enter opens the current (selected)
tile the same way. Right-clicking a tile opens a context menu with actions
on the selection (rotate, convert to MP4).
"""

from __future__ import annotations

import logging
import os
import sys
import time

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import (
    QCursor,
    QDrag,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QAbstractItemView, QListView, QMenu

from .. import config
from ..media.player import PreviewPlayer
from ..media.converter import is_mp4_like
from ..media.rotator import RotateDirection
from ..media.seek_bar import WIDGET_HEIGHT as SEEK_STRIP_HEIGHT
from ..models.video_model import VideoModel
from ..open_external import open_externally
from .video_delegate import VideoDelegate, cell_height

log = logging.getLogger(__name__)

#: Temporary pointer-event tracing for diagnosing hover/scrub issues.
#: Run with VIDEO_PREVIEWER_DEBUG_POINTER=1 to enable.
_DEBUG_POINTER = bool(os.environ.get("VIDEO_PREVIEWER_DEBUG_POINTER"))

# QDrag's default action is not enough to override Explorer's contextual
# copy/move choice (notably across volumes). This native clipboard format is
# how a Windows drag source explicitly asks the Shell for a move. Its payload
# is a little-endian DWORD containing DROPEFFECT_MOVE (2).
_IS_WINDOWS = sys.platform == "win32"
_PREFERRED_DROP_EFFECT = (
    'application/x-qt-windows-mime;value="Preferred DropEffect"'
)
_DROPEFFECT_MOVE = QByteArray(b"\x02\x00\x00\x00")


class VideoGrid(QListView):
    #: Emitted when the user double-clicks empty grid space (no video tile
    #: under the pointer); the window opens its folder picker in response.
    open_folder_requested = Signal()
    #: Emitted on F2 while one or more tiles are selected; the window runs
    #: the rename action on the current selection.
    rename_requested = Signal()
    #: Emitted on Delete while tiles are selected. True means Shift+Delete
    #: requested permanent deletion; False means move to the system trash.
    delete_requested = Signal(bool)
    #: Emitted from the tile context menu with a :class:`RotateDirection`;
    #: the window rotates the selected videos.
    rotate_requested = Signal(object)
    #: Emitted from the tile context menu: convert the selection to MP4.
    convert_requested = Signal()
    #: A tile drag began with these source paths (before the modal drag
    #: loop runs). The window uses this to reset its "drop handled internally"
    #: latch so it can tell a sidebar drop from a file-manager drop below.
    drag_started = Signal(list)
    #: A tile drag ended without the sidebar handling it: the files were
    #: dropped on a file manager, which copied/moved them itself. Carries the
    #: source paths and the action the drag actually executed. The window
    #: reconciles the grid against the disk (the action is not a reliable
    #: signal across platforms, so existence is) and completes a source-side
    #: move when ``QDrag.exec()`` returns ``MoveAction``.
    drag_finished = Signal(list, Qt.DropAction)

    def __init__(self, model: VideoModel, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self._model = model
        self._player: PreviewPlayer | None = None
        self._delegate = VideoDelegate(self)
        self.setItemDelegate(self._delegate)
        self._hover_row: int = -1
        # Set while the window is tearing down: ``closeEvent`` keeps pumping
        # the event queue (to let workers drain) with the window still on
        # screen, and a stray hover/press there must stay inert.
        self._closing = False
        # Double-click-to-open state. Detected from consecutive presses
        # because Qt's own MouseButtonDblClick synthesis is unreliable here:
        # the native QVideoWidget window showing under the cursor mid-sequence
        # can reset the OS click detection between the two clicks.
        self._last_press: tuple[int, QPoint, float] | None = None  # (row, pos, time)
        self._last_open: tuple[int, float] | None = None  # (row, time)
        self._last_folder_open: float | None = None  # empty-space request
        # Row under the current left-button press (-1: empty space), and whether
        # that press began on the timeline strip. Both gate ``startDrag``:
        # a strip press is the *scrub* gesture and must never be
        # reinterpreted as a drag (Qt's default would start one the moment
        # the pointer moves past the drag distance), and a press on empty
        # space belongs to the rubber band, not to a file drag.
        self._press_row: int = -1
        self._press_on_strip = False

        # A re-sort moves items between rows: without dropping the pointer
        # state the muted preview would keep playing the previous item's file,
        # pinned over whatever tile moved under the cursor.
        model.layoutChanged.connect(self.clear_hover)

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setMovement(QListView.Movement.Static)
        # Click / Ctrl+click / Shift+click / rubber band select tiles; the
        # window runs actions (F2 rename, Delete) off this selection. Focus
        # lets the grid receive the key presses those actions use.
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setSpacing(config.GRID_SPACING)
        # Drag the selected tiles out as file URLs: onto a sidebar folder
        # (the window moves them) or onto a file-manager window (Explorer /
        # Finder performs the copy/move itself). Drag-only: the grid never
        # accepts drops. Rubber-band selection is unaffected.
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._apply_grid_size(self.viewport().width())

        self.viewport().installEventFilter(self)

    # -- wiring ------------------------------------------------------------------

    def set_player(self, player: PreviewPlayer) -> None:
        self._player = player

    def delegate(self) -> VideoDelegate:
        return self._delegate

    def clear_hover(self) -> None:
        """Forget all pointer state and stop any preview.

        Call when the model content changes wholesale (folder switch): the
        row index under the cursor may be reused by a *different* item, and
        without the reset ``_on_pointer_move`` sees ``row == _hover_row``
        and skips ``enter()``, leaving the tile under the pointer dead.
        """
        self._clear_hover()
        self._last_press = None
        self._last_open = None
        self._last_folder_open = None

    def set_closing(self) -> None:
        """Stop reacting to the pointer for good (the window is closing).

        Without this, the drain loop in ``closeEvent`` can deliver a hover
        that restarts the shared player right after shutdown stopped it, or a
        press pair that launches an external player mid-close.
        """
        self._closing = True
        self.clear_hover()

    # -- responsive layout ---------------------------------------------------------

    def _apply_grid_size(self, viewport_width: int) -> None:
        if viewport_width <= 0:
            return
        # QListView IconMode tiles cells back-to-back (setSpacing is ignored
        # with an explicit grid size), so the gutter is part of the cell
        # pitch: the delegate draws the card inset by GRID_SPACING on the
        # right and bottom, which yields the visible gutter.
        cols = max(
            config.MIN_COLS,
            min(
                config.MAX_COLS,
                viewport_width // (config.CELL_WIDTH + config.GRID_SPACING),
            ),
        )
        pitch = max(
            config.GRID_SPACING + 80,
            (viewport_width - 2 * config.GRID_SPACING) // cols,
        )
        w = pitch - config.GRID_SPACING
        self.setGridSize(QSize(pitch, cell_height(w) + config.GRID_SPACING))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_grid_size(self.viewport().width())
        self._reposition_preview()

    # -- pointer handling -------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.viewport() and not self._closing:
            etype = event.type()
            if etype == QEvent.Type.MouseMove:
                pos = event.position().toPoint()
                if _DEBUG_POINTER:
                    index = self.indexAt(pos)
                    row = index.row() if index.isValid() else -1
                    strip = (
                        self._in_timeline(pos, self._cell_rect(row))
                        if row >= 0
                        else None
                    )
                    true = self.viewport().mapFromGlobal(QCursor.pos())
                    log.info(
                        "ptr move pos=(%d,%d) true=(%d,%d) buttons=%d row=%d "
                        "hover=%d strip=%s cell_bottom=%s",
                        pos.x(), pos.y(), true.x(), true.y(),
                        event.buttons().value,
                        row, self._hover_row, strip,
                        self._cell_rect(row).bottom() if row >= 0 else None,
                    )
                self._on_pointer_move(pos)
            elif etype == QEvent.Type.Leave:
                # macOS fires a phantom Leave at the viewport whenever the
                # video widget's native surface appears under the cursor
                # (AppKit re-runs enter/leave tracking around it), and again
                # when a mouse grab ends — even though the pointer never
                # left the grid. Trusting it killed the preview the instant
                # it started (flicker + autoplay never firing while the
                # pointer moved) and hid the seek bar mid-scrub. So treat
                # Leave as provisional: only act when the real cursor has
                # actually exited the viewport; otherwise the next MouseMove
                # re-evaluates the row anyway.
                inside = self.viewport().rect().contains(
                    self.viewport().mapFromGlobal(QCursor.pos())
                )
                if _DEBUG_POINTER:
                    log.info(
                        "ptr LEAVE viewport (cursor still inside=%s, gpos=%s)"
                        " -> %s",
                        inside, QCursor.pos(),
                        "ignored" if inside else "clear hover",
                    )
                if not inside:
                    self._clear_hover()
            elif etype == QEvent.Type.Enter:
                if _DEBUG_POINTER:
                    log.info("ptr ENTER viewport gpos=%s", QCursor.pos())
            elif etype == QEvent.Type.MouseButtonPress:
                if _DEBUG_POINTER:
                    p = event.position().toPoint()
                    log.info("ptr press pos=(%d,%d)", p.x(), p.y())
                self._on_press(event.position().toPoint())
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            event.ignore()
            return
        if event.key() == Qt.Key.Key_F2:
            if self.selectionModel().hasSelection():
                self.rename_requested.emit()
            return
        if event.key() == Qt.Key.Key_Delete:
            if self.selectionModel().hasSelection():
                permanent = bool(
                    event.modifiers() & Qt.KeyboardModifier.ShiftModifier
                )
                self.delete_requested.emit(permanent)
            return
        if event.key() == Qt.Key.Key_Escape:
            # Escape clears the selection (Explorer-like); never steal it
            # from an open dialog — a modal dialog takes keys first.
            self.clearSelection()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            # Enter opens the selected tile with the OS player (Explorer-
            # like). The view auto-sets a current index without a real
            # selection, so require hasSelection(); reuse the double-click
            # dedupe so one gesture never opens twice.
            sel = self.selectionModel()
            current = sel.currentIndex()
            if sel.hasSelection() and current.isValid():
                self._open_item(current.row())
            return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            event.ignore()
            return
        menu = self.context_menu_at(event.pos())
        if menu is None:
            return
        self._clear_hover()  # the preview must not keep playing under the menu
        menu.exec(event.globalPos())
        menu.deleteLater()

    def context_menu_at(self, pos: QPoint) -> QMenu | None:
        """Menu for a right-click at viewport *pos* (None on empty space).

        Explorer-like: right-clicking a tile outside the selection makes it
        the selection; right-clicking inside keeps the whole selection.
        """
        index = self.indexAt(pos)
        if not index.isValid():
            return None
        sel = self.selectionModel()
        if not sel.isSelected(index):
            sel.select(
                index, sel.SelectionFlag.ClearAndSelect | sel.SelectionFlag.Rows
            )
            sel.setCurrentIndex(index, sel.SelectionFlag.NoUpdate)
        menu = QMenu(self)
        rotate = menu.addMenu("Rotate")
        for direction in RotateDirection:
            action = rotate.addAction(direction.label)
            action.triggered.connect(
                lambda _=False, d=direction: self.rotate_requested.emit(d)
            )
        convert = menu.addAction("Convert to MP4")
        convert.triggered.connect(lambda _=False: self.convert_requested.emit())
        # Nothing to do when every selected video already is MP4.
        convert.setEnabled(any(
            item is not None and not is_mp4_like(item.path)
            for item in (self._model.item_at(i.row()) for i in sel.selectedRows())
        ))
        return menu

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # Qt's own double-click delivery (kept for the cases where it works).
        if self._closing:
            event.ignore()
            return
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            self._request_open_folder()
            return
        self._open_item(index.row())

    def _dbl_interval_ms(self) -> int:
        return QGuiApplication.styleHints().mouseDoubleClickInterval()

    def _on_press(self, pos: QPoint) -> None:
        """Detect a double-click from consecutive left presses.

        Qt's MouseButtonDblClick synthesis can miss the second click (the
        native video widget appearing under the cursor resets the OS click
        sequence), so a same-tile press pair within the OS double-click
        interval and drift opens the video directly. The same pair on empty
        grid space (no video under the pointer) requests the folder picker
        instead.
        """
        index = self.indexAt(pos)
        row = index.row() if index.isValid() else -1
        # Remember where this press began so ``canStartDrag`` can refuse to
        # turn a strip-press (scrub) or an empty-space press (rubber band)
        # into a file drag.
        self._press_row = row
        self._press_on_strip = row >= 0 and self._in_timeline(
            pos, self._cell_rect(row)
        )
        now = time.monotonic()
        prev = self._last_press
        self._last_press = (row, pos, now)
        if prev is None or prev[0] != row:
            return
        if (now - prev[2]) * 1000 > self._dbl_interval_ms():
            return
        if (pos - prev[1]).manhattanLength() > config.DOUBLE_CLICK_MAX_DIST:
            return
        self._last_press = None
        if row < 0:
            self._request_open_folder()
        else:
            self._open_item(row)

    def _open_item(self, row: int) -> None:
        """Open the video at *row* with the OS-default player (once)."""
        item = self._model.item_at(row)
        if item is None:
            return
        # A triple-click delivers MouseButtonDblClick twice (and the
        # press-based path can also race it): open exactly once per gesture.
        now = time.monotonic()
        last = self._last_open
        if (
            last is not None
            and last[0] == row
            and (now - last[1]) * 1000 <= 2 * self._dbl_interval_ms()
        ):
            return
        self._last_open = (row, now)
        self._clear_hover()  # stop the inline preview before handing off
        open_externally(item.path)

    def _request_open_folder(self) -> None:
        """Empty-space double-click: ask the window to open a folder (once)."""
        # Same per-gesture dedupe as ``_open_item``: a triple click delivers
        # MouseButtonDblClick twice (and the press-based path can race it).
        now = time.monotonic()
        last = self._last_folder_open
        if (
            last is not None
            and (now - last) * 1000 <= 2 * self._dbl_interval_ms()
        ):
            return
        self._last_folder_open = now
        self.open_folder_requested.emit()

    def _on_pointer_move(self, pos: QPoint) -> None:
        if self._player is None:
            return
        index = self.indexAt(pos)
        row = index.row() if index.isValid() else -1
        if row != self._hover_row:
            if row >= 0:
                self._clear_hover()
                self._hover_row = row
                item = self._model.item_at(row)
                if item is not None:
                    self._player.enter(str(item.path), self._cell_rect(row))
            else:
                self._clear_hover()
        elif row >= 0:
            cell = self._cell_rect(row)
            if self._in_timeline(pos, cell):
                self._player.scrub(self._fraction_at(pos, cell))

    def _clear_hover(self) -> None:
        if self._hover_row >= 0:
            self._hover_row = -1
            if self._player is not None:
                self._player.leave()

    def _cell_rect(self, row: int) -> QRect:
        # Card rect without the gutter: the preview video + seek bar align
        # with the painted card, not the full cell pitch.
        return self.visualRect(self._model.index(row)).adjusted(
            0, 0, -config.GRID_SPACING, -config.GRID_SPACING
        )

    @staticmethod
    def _fraction_at(pos: QPoint, cell: QRect) -> float:
        if cell.width() <= 0:
            return 0.0
        return max(0.0, min(1.0, (pos.x() - cell.left()) / cell.width()))

    @staticmethod
    def _in_timeline(pos: QPoint, cell: QRect) -> bool:
        """True when *pos* sits in the seek-bar strip at the cell's bottom.

        Matches the ``SeekBarOverlay`` geometry: pinned to the bottom edge
        of the video widget and ``SEEK_STRIP_HEIGHT`` px tall. Scrubbing
        outside the strip would hijack every pass over the thumbnail.
        """
        # QRect::bottom() already excludes the phantom extra row, so the
        # strip spans [bottom - height + 1, bottom].
        return pos.y() > cell.bottom() - SEEK_STRIP_HEIGHT

    # -- drag out (move selected videos to a folder) ------------------------------

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        """Drag the selected tiles out as real file URLs.

        Copy *or* move, defaulting to move: a sidebar folder moves the files
        (the window performs the move + cache migration), while a file-manager
        window (Explorer / Finder) performs the copy/move itself.
        ``drag_finished`` then lets the window reconcile the grid against
        the disk for dragged-out files (a file manager may have moved them
        out from under us).

        Gesture guard first — and it must live here, not in ``canStartDrag``:
        Qt 6 reaches ``startDrag`` straight from ``mouseMoveEvent`` (private
        ``maybeStartDrag``) without consulting ``canStartDrag`` at all. A
        press that began on the timeline strip is the *scrub* gesture and a
        press on empty space belongs to the rubber band; neither may become
        a file drag.
        """
        if self._closing or self._press_on_strip or self._press_row < 0:
            return
        rows = self.selectionModel().selectedRows()
        paths = [
            item.path
            for idx in rows
            if (item := self._model.item_at(idx.row())) is not None
        ]
        if not paths:
            return
        mime = self._model.mimeData(self.selectionModel().selectedIndexes())
        if not mime.hasUrls():
            mime.deleteLater()
            return
        if _IS_WINDOWS:
            # Tell Explorer this is a move at the native Shell layer. Without
            # CFSTR_PREFERREDDROPEFFECT it may choose CopyAction despite the
            # MoveAction default passed to QDrag.exec(), leaving originals.
            mime.setData(_PREFERRED_DROP_EFFECT, _DROPEFFECT_MOVE)
        self._clear_hover()  # don't keep a preview playing while dragging
        if self._player is not None:
            # Release the previewed file before the drag starts: on Windows
            # a loaded source keeps it locked, so Explorer would copy the
            # dragged file and then fail to delete the original ("in use").
            # ``_clear_hover`` only reaches the player while a tile is
            # hovered, so ask for the release unconditionally.
            self._player.leave()
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = self._drag_pixmap(len(paths))
        if pixmap is not None:
            drag.setPixmap(pixmap)
            drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        self.drag_started.emit(paths)
        executed = drag.exec(
            Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
            Qt.DropAction.MoveAction,
        )
        if _DEBUG_POINTER:
            log.info("drag executed action=%s for %d file(s)", executed, len(paths))
        self.drag_finished.emit(paths, executed)

    def _drag_pixmap(self, selected_count: int) -> QPixmap | None:
        """Return a slim, translucent label to ride on the drag cursor.

        A grabbed tile is almost as wide as the sidebar and hides its target
        highlight.  This deliberately carries only the filename (and an
        optional selection count), keeping the drag identity useful without
        obscuring the folder tree.
        """
        index = self.currentIndex()
        if not index.isValid():
            return None
        item = self._model.item_at(index.row())
        if item is None:
            return None

        width = config.DRAG_PREVIEW_WIDTH
        height = config.DRAG_PREVIEW_HEIGHT
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        background = palette.base().color()
        background.setAlpha(210)
        border = palette.highlight().color()
        border.setAlpha(225)
        text_color = palette.text().color()
        text_color.setAlpha(245)

        pill = pixmap.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(border, 1))
        painter.setBrush(background)
        painter.drawRoundedRect(pill, height // 2, height // 2)

        label = item.filename
        if selected_count > 1:
            label = f"{label}  +{selected_count - 1}"
        text_rect = pill.adjusted(9, 0, -9, 0)
        painter.setFont(self.font())
        metrics = QFontMetrics(painter.font())
        label = metrics.elidedText(
            label, Qt.TextElideMode.ElideMiddle, text_rect.width()
        )
        painter.setPen(text_color)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label,
        )
        painter.end()
        return pixmap

    # -- keep the active preview pinned to its tile while scrolling -------------------

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._reposition_preview()

    def _reposition_preview(self) -> None:
        if self._player is not None and self._hover_row >= 0:
            self._player.reposition(self._cell_rect(self._hover_row))
