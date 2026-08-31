"""Responsive thumbnail grid (QListView + model/view + custom delegate).

Pointer handling (via an event filter on the viewport) maps each tile to:
  * enter  -> start hover preview (muted autoplay after a short delay)
  * move   -> horizontal position scrubs the shared player (throttled)
  * leave  -> stop the player, restore the static thumbnail
"""

from __future__ import annotations

import time

from PySide6.QtCore import QEvent, QModelIndex, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QListView

from .. import config
from ..media.player import PreviewPlayer
from ..models.video_model import VideoModel
from ..open_external import open_externally
from .video_delegate import VideoDelegate, cell_height


class VideoGrid(QListView):
    def __init__(self, model: VideoModel, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)
        self._model = model
        self._player: PreviewPlayer | None = None
        self._delegate = VideoDelegate(self)
        self.setItemDelegate(self._delegate)
        self._hover_row: int = -1
        # Double-click-to-open state. Detected from consecutive presses
        # because Qt's own MouseButtonDblClick synthesis is unreliable here:
        # the native QVideoWidget window showing under the cursor mid-sequence
        # can reset the OS click detection between the two clicks.
        self._last_press: tuple[int, QPoint, float] | None = None  # (row, pos, time)
        self._last_open: tuple[int, float] | None = None  # (row, time)

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setMovement(QListView.Movement.Static)
        self.setSelectionMode(QListView.SelectionMode.NoSelection)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        if obj is self.viewport():
            etype = event.type()
            if etype == QEvent.Type.MouseMove:
                self._on_pointer_move(event.position().toPoint())
            elif etype == QEvent.Type.Leave:
                self._clear_hover()
            elif etype == QEvent.Type.MouseButtonPress:
                self._on_press(event.position().toPoint())
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # Qt's own double-click delivery (kept for the cases where it works).
        index = self.indexAt(event.position().toPoint())
        if not index.isValid():
            super().mouseDoubleClickEvent(event)
            return
        self._open_item(index.row())

    def _dbl_interval_ms(self) -> int:
        return QGuiApplication.styleHints().mouseDoubleClickInterval()

    def _on_press(self, pos: QPoint) -> None:
        """Detect a double-click from consecutive left presses.

        Qt's MouseButtonDblClick synthesis can miss the second click (the
        native video widget appearing under the cursor resets the OS click
        sequence), so a same-tile press pair within the OS double-click
        interval and drift opens the video directly.
        """
        index = self.indexAt(pos)
        row = index.row() if index.isValid() else -1
        now = time.monotonic()
        prev = self._last_press
        self._last_press = (row, pos, now)
        if row < 0 or prev is None or prev[0] != row:
            return
        if (now - prev[2]) * 1000 > self._dbl_interval_ms():
            return
        if (pos - prev[1]).manhattanLength() > config.DOUBLE_CLICK_MAX_DIST:
            return
        self._last_press = None
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
            self._player.scrub(self._fraction_at(pos, self._cell_rect(row)))

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

    # -- keep the active preview pinned to its tile while scrolling -------------------

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        self._reposition_preview()

    def _reposition_preview(self) -> None:
        if self._player is not None and self._hover_row >= 0:
            self._player.reposition(self._cell_rect(self._hover_row))
