"""QAbstractListModel backing the video grid.

Rows are stable: items are updated in place (``dataChanged``) while
metadata/thumbnails arrive asynchronously, so view indices never go stale.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from .video_item import VideoItem, normalize_path


class VideoModel(QAbstractListModel):
    PathRole = Qt.UserRole + 1
    FilenameRole = Qt.UserRole + 2
    DurationRole = Qt.UserRole + 3
    WidthRole = Qt.UserRole + 4
    HeightRole = Qt.UserRole + 5
    ThumbReadyRole = Qt.UserRole + 6
    ThumbnailPathRole = Qt.UserRole + 7
    VidRole = Qt.UserRole + 8

    _ALL_ROLES = [
        Qt.DisplayRole,
        PathRole,
        FilenameRole,
        DurationRole,
        WidthRole,
        HeightRole,
        ThumbReadyRole,
        ThumbnailPathRole,
        VidRole,
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: list[VideoItem] = []
        self._rows: dict[str, int] = {}  # normalized path -> row

    # -- Qt model API -----------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._items)
        return 0

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self._items):
            return None
        item = self._items[index.row()]
        if role in (Qt.DisplayRole, self.FilenameRole):
            return item.filename
        if role == self.PathRole:
            return item.path
        if role == self.DurationRole:
            return item.duration_ms
        if role == self.WidthRole:
            return item.width
        if role == self.HeightRole:
            return item.height
        if role == self.ThumbReadyRole:
            return item.thumb_ready
        if role == self.ThumbnailPathRole:
            return item.thumbnail_path
        if role == self.VidRole:
            return item.vid
        return None

    def flags(self, index: QModelIndex):
        return Qt.ItemIsEnabled  # not selectable: the grid is purely visual

    # -- Mutations (main thread only) --------------------------------------

    def add_items(self, items: list[VideoItem]) -> list[int]:
        """Append items, skipping paths already present. Returns added rows."""
        if not items:
            return []
        start = len(self._items)
        added: list[int] = []
        for item in items:
            key = normalize_path(item.path)
            if key in self._rows:
                continue
            row = len(self._items)
            self._items.append(item)
            self._rows[key] = row
            added.append(row)
        if not added:
            return []
        self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
        self.endInsertRows()
        return added

    def update_item(self, row: int, item: VideoItem) -> None:
        if not 0 <= row < len(self._items):
            return
        self._items[row] = item
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, list(self._ALL_ROLES))

    def item_at(self, row: int) -> VideoItem | None:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def row_for(self, path: Path) -> int | None:
        return self._rows.get(normalize_path(path))

    def count(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        if not self._items:
            return
        self.beginRemoveRows(QModelIndex(), 0, len(self._items) - 1)
        self._items.clear()
        self._rows.clear()
        self.endRemoveRows()
