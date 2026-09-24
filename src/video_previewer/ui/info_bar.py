"""Thin info bar under the grid: extra facts about the current view.

Currently shows the selection's file count and total size on disk.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ..models.video_model import VideoModel
from .settings_dialog import format_byte_size
from .video_grid import VideoGrid


class InfoBar(QWidget):
    """Follows the grid's selection and model; recomputes lazily.

    Selection and data changes can arrive in bursts (rubber-band drags,
    thumbnail batches), so every trigger only (re)starts a zero-delay
    single-shot timer and the O(selection) sum runs once per burst.
    """

    def __init__(self, grid: VideoGrid, model: VideoModel, parent=None) -> None:
        super().__init__(parent)
        self._grid = grid
        self._model = model
        self.setObjectName("infoBar")

        self._selection_label = QLabel("", self)
        self._selection_label.setObjectName("infoBarSelection")
        self._selection_label.setStyleSheet("color: #9a9aa5;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 0)
        layout.setSpacing(12)
        layout.addWidget(self._selection_label)
        layout.addStretch(1)
        self.setFixedHeight(self._selection_label.sizeHint().height() + 2)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self.refresh)
        grid.selectionModel().selectionChanged.connect(self._schedule)
        # A rotate/convert rewrites sizes; removals may not emit
        # selectionChanged for the rows they drop.
        model.dataChanged.connect(self._schedule)
        model.rowsRemoved.connect(self._schedule)
        model.modelReset.connect(self._schedule)
        self.refresh()

    def _schedule(self, *_args) -> None:
        self._timer.start()

    def refresh(self) -> None:
        """Recompute the selection summary now."""
        self._timer.stop()
        count = 0
        total = 0
        for index in self._grid.selectionModel().selectedRows():
            item = self._model.item_at(index.row())
            if item is not None:
                count += 1
                total += item.size
        if count == 0:
            self._selection_label.setText("No videos selected")
            return
        noun = "video" if count == 1 else "videos"
        self._selection_label.setText(
            f"{count} {noun} selected · {format_byte_size(total)}"
        )

    @property
    def selection_text(self) -> str:
        return self._selection_label.text()
