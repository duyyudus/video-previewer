"""QAbstractListModel backing the video grid.

Rows are stable in content: items are updated in place (``dataChanged``)
while metadata/thumbnails arrive asynchronously. What is *not* stable is
which row an item sits on — any mutation may reorder the rows (see
:mod:`.sorting`), so callers must resolve a row with :meth:`row_for`
immediately before using it and never carry a row number across mutations.
A reorder goes out as ``layoutChanged`` with the persistent indices
remapped, so views keep their scroll position.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QMimeData,
    Qt,
    QUrl,
)

from .sorting import SortKey, SortOrder, SortValue, item_sort_key
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
        # Sort state: _keys is parallel to _items, and both are kept in
        # display order (except while a reorder is deferred), so appending is
        # cheap and reordering is a permutation.
        self._keys: list[SortValue] = []
        self._sort_key: SortKey = SortKey.NAME
        self._sort_order: SortOrder = SortOrder.ASC
        # Deferred reorder (see set_layout_deferred): while a scan streams in,
        # rows stay where they are and only the pending flag is remembered.
        self._defer_layout = False
        self._layout_pending = False

    # -- Qt model API -----------------------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        if not parent.isValid():
            return len(self._items)
        return 0

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> object:
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

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        # Selectable: the grid drives further actions off the selection
        # (F2 rename today). Draggable: the grid drags the selection out
        # as file URLs (the sidebar moves them, a file manager copies or
        # moves natively). Items stay non-editable.
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
        )

    # -- drag payload --------------------------------------------------------

    def mimeTypes(self) -> list[str]:  # noqa: N802
        return ["text/uri-list"]

    def mimeData(  # noqa: N802
        self, indexes: list[QModelIndex]
    ) -> QMimeData:
        """File URLs for the dragged rows (rule: real paths, not display text).

        ``text/uri-list`` is what Explorer/Finder need to move or copy the
        actual files, and what the sidebar's drop handler reads back to know
        which videos were dragged. A plain ``QAbstractListModel`` would put
        the DisplayRole *filename* here, which the file manager cannot
        resolve to a source file.
        """
        data = QMimeData()
        urls: list[QUrl] = []
        for index in indexes:
            if not index.isValid():
                continue
            path = self.data(index, self.PathRole)
            if path is not None:
                urls.append(QUrl.fromLocalFile(str(path)))
        data.setUrls(urls)
        return data

    # -- sorting ------------------------------------------------------------

    @property
    def sort_key(self) -> SortKey:
        return self._sort_key

    @property
    def sort_order(self) -> SortOrder:
        return self._sort_order

    def set_sort(self, key: SortKey | str, order: SortOrder | str) -> None:
        """Order the rows by *key*/*order* (no-op when nothing moves).

        Always immediate, even with layout deferred: this is the user asking
        for a new order, and waiting for a scan to finish would look broken.
        """
        key, order = SortKey(key), SortOrder(order)  # unknown values raise
        if key is self._sort_key and order is self._sort_order:
            return
        self._sort_key, self._sort_order = key, order
        self._keys = [self._key_of(item) for item in self._items]
        self._resort(force=True)

    def set_layout_deferred(self, deferred: bool) -> None:
        """Postpone reordering while a batch of mutations is in flight.

        A scan delivers a big folder in small batches, and re-permuting the
        whole model (plus a full view relayout) per batch is quadratic in the
        file count: 10k files at the default batch size costs ~150 relayouts
        and seconds of frozen GUI whenever the sort disagrees with the
        scanner's delivery order. While deferred, rows are simply appended in
        arrival order — every row number stays valid, only the visual order
        lags — and turning deferral off applies the order once.
        """
        deferred = bool(deferred)
        if deferred == self._defer_layout:
            return
        self._defer_layout = deferred
        if not deferred and self._layout_pending:
            self._resort(force=True)

    def _key_of(self, item: VideoItem) -> SortValue:
        return item_sort_key(item, self._sort_key, self._sort_order)

    def _ordered_from(self, first: int) -> bool:
        """Report whether the tail starting at *first* keeps the order.

        Appends land at the end of an already-ordered list, so a scan that
        delivers items in the sorted order (the scanner's own name sort) only
        costs one comparison per new item instead of a full re-sort. Checking
        the tail is enough because the prefix is ordered by invariant: while
        layout is deferred, the batch that first broke the order already set
        the pending flag.
        """
        keys = self._keys
        i = max(first, 1)
        while i < len(keys):
            if keys[i] < keys[i - 1]:
                return False
            i += 1
        return True

    def _resort(self, force: bool = False) -> bool:
        """Permute the rows into sort order (``layoutChanged``, not a reset).

        Returns whether a layout change was broadcast. A model reset would be
        simpler but throws the view's scroll position away; a layout change
        with remapped persistent indices moves the rows without disturbing the
        viewport.
        """
        if self._defer_layout and not force:
            self._layout_pending = True
            return False
        self._layout_pending = False
        n = len(self._items)
        if n < 2:
            return False
        order = sorted(range(n), key=self._keys.__getitem__)
        if all(row == pos for pos, row in enumerate(order)):
            return False  # already ordered: no signal, no repaint
        # Where each *old* row ends up, for the persistent-index remap.
        position = [0] * n
        for new_row, old_row in enumerate(order):
            position[old_row] = new_row
        before = [self.index(row) for row in range(n)]
        self.layoutAboutToBeChanged.emit()
        self._items = [self._items[row] for row in order]
        self._keys = [self._keys[row] for row in order]
        self._rows = {normalize_path(it.path): row for row, it in enumerate(self._items)}
        self.changePersistentIndexList(before, [self.index(row) for row in position])
        self.layoutChanged.emit()
        return True

    # -- Mutations (main thread only) --------------------------------------

    def add_items(self, items: list[VideoItem]) -> list[int]:
        """Add items, skipping paths already present.

        Returns the rows the added items ended up on (in argument order);
        with a sort active those are not necessarily consecutive, because
        the batch is placed into the ordered rows before returning. The rows
        of items that were already in the model may have moved too. Callers
        that go on to touch other rows must look them up again first.
        """
        if not items:
            return []
        # Stage first: Qt's contract requires the model to be unmodified
        # until beginInsertRows has been called (a proxy/view querying
        # rowCount() from rowsAboutToBeInserted must see consistent state).
        fresh: list[tuple[str, VideoItem]] = []
        seen: set[str] = set()
        for item in items:
            key = normalize_path(item.path)
            if key in self._rows or key in seen:
                continue
            seen.add(key)
            fresh.append((key, item))
        if not fresh:
            return []
        start = len(self._items)
        self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
        for key, item in fresh:
            row = len(self._items)
            self._items.append(item)
            self._keys.append(self._key_of(item))
            self._rows[key] = row
        self.endInsertRows()
        if not self._ordered_from(start):
            self._resort()
        return [self._rows[key] for key, _ in fresh]

    def update_item(self, row: int, item: VideoItem) -> None:
        """Replace the item on *row*.

        *row* must come from a :meth:`row_for` lookup made immediately before
        this call — updating an item can move it, so an older row number may
        belong to a different file by now.
        """
        if not 0 <= row < len(self._items):
            return
        previous = self._items[row]
        self._items[row] = item
        # Late metadata never moves a row; only a sort key that actually
        # changed does, i.e. a re-scanned file's mtime under a date sort (the
        # name sort keys off the path, which cannot change in place).
        moved = (
            self._sort_key is SortKey.MODIFIED and previous.modified != item.modified
        )
        if moved:
            self._keys[row] = self._key_of(item)
            if self._resort():
                return  # layoutChanged already repaints every visible row
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, list(self._ALL_ROLES))

    def update_items(self, items: list[VideoItem]) -> None:
        """Refresh the rows currently holding these items' paths.

        The batch entry point for updates: it resolves each row only when it
        is about to write it (a sibling update may already have reordered the
        rows) and reorders once for the whole batch instead of once per file.
        Items whose file is no longer in the model are skipped.
        """
        outer = self._defer_layout  # nest inside a running scan without
        self._defer_layout = True   # releasing its deferral on the way out
        try:
            for item in items:
                row = self._rows.get(normalize_path(item.path))
                if row is not None:
                    self.update_item(row, item)
        finally:
            self._defer_layout = outer
            if not outer and self._layout_pending:
                self._resort(force=True)

    def rename_item(self, row: int, item: VideoItem) -> None:
        """Replace the item on *row* with one whose *path* changed.

        Used by the rename action: unlike :meth:`update_item` (same path,
        new metadata) this swaps the identity, so the path -> row map gets
        the old key dropped and the new one added, and the row is resorted
        (the name sort usually moves it). The persistent-index remap in
        ``_resort`` makes the selection follow the renamed tile.
        """
        if not 0 <= row < len(self._items):
            return
        previous = self._items[row]
        self._rows.pop(normalize_path(previous.path), None)
        self._items[row] = item
        self._rows[normalize_path(item.path)] = row
        self._keys[row] = self._key_of(item)
        if not self._resort():
            idx = self.index(row)
            self.dataChanged.emit(idx, idx, list(self._ALL_ROLES))

    def remove_items(self, paths: list[Path]) -> int:
        """Drop the rows currently holding *paths* (unknown paths skipped).

        Used when a dragged file leaves the grid's scope: the tile must
        disappear with the file, not linger as a dead ghost until the next
        scan. Returns the number of rows removed. Contiguous runs go out
        as one block, and blocks are processed bottom-up so a removal
        never shifts the row numbers of the blocks still to come.
        """
        rows = sorted(
            {
                row
                for row in (self._rows.get(normalize_path(p)) for p in paths)
                if row is not None
            }
        )
        removed = 0
        i = len(rows)
        while i > 0:
            last = rows[i - 1]
            first = last
            while i > 1 and rows[i - 2] == first - 1:
                i -= 1
                first -= 1
            self.beginRemoveRows(QModelIndex(), first, last)
            del self._items[first : last + 1]
            del self._keys[first : last + 1]
            self._rows = {
                normalize_path(it.path): row for row, it in enumerate(self._items)
            }
            self.endRemoveRows()
            removed += last - first + 1
            i -= 1
        return removed

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
        self._keys.clear()
        self._rows.clear()
        self._layout_pending = False  # nothing left to put in order
        self.endRemoveRows()
