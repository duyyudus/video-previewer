"""Responsive thumbnail grid (QListView + model/view + custom delegate).

Pointer handling (via an event filter on the viewport) maps each tile to:
  * enter  -> start hover preview (muted autoplay after a short delay)
  * move   -> within the timeline strip at the tile's bottom edge, the
    horizontal position scrubs the shared player (throttled)
  * leave  -> stop the player, restore the static thumbnail

A double-click opens the tile's video with the OS-default player; on empty
grid space (no video under the pointer) it emits ``open_folder_requested``
so the window can offer its folder picker. Enter opens the current (selected)
tile the same way.
"""

from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QListView

from .. import config
from ..media.player import PreviewPlayer
from ..media.seek_bar import WIDGET_HEIGHT as SEEK_STRIP_HEIGHT
from ..models.video_model import VideoModel
from ..open_external import open_externally
from .video_delegate import VideoDelegate, cell_height

log = logging.getLogger(__name__)

#: Temporary pointer-event tracing for diagnosing hover/scrub issues.
#: Run with VIDEO_PREVIEWER_DEBUG_POINTER=1 to enable.
_DEBUG_POINTER = bool(os.environ.get("VIDEO_PREVIEWER_DEBUG_POINTER"))


class VideoGrid(QListView):
    #: Emitted when the user double-clicks empty grid space (no video tile
    #: under the pointer); the window opens its folder picker in response.
    open_folder_requested = Signal()
    #: Emitted on F2 while one or more tiles are selected; the window runs
    #: the rename action on the current selection.
    rename_requested = Signal()

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
        # window runs actions (F2 rename) off this selection. Focus lets the
        # grid receive the key presses those actions use.
        self.setSelectionMode(QListView.SelectionMode.ExtendedSelection)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setSpacing(config.GRID_SPACING)
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

    # -- keep the active preview pinned to its tile while scrolling -------------------

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._reposition_preview()

    def _reposition_preview(self) -> None:
        if self._player is not None and self._hover_row >= 0:
            self._player.reposition(self._cell_rect(self._hover_row))
