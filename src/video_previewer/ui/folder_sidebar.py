"""Folder-navigation sidebar.

A directory tree (``QTreeView`` over a directories-only ``QFileSystemModel``)
for browsing the filesystem and picking the folder shown in the grid. The
model loads directory contents lazily, so browsing never blocks the GUI
thread, and the tree keeps itself current when folders appear or disappear
on disk.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QTreeView,
    QWidget,
)


class FolderSidebar(QTreeView):
    """Directory tree for choosing the folder shown in the grid.

    Clicking a folder selects it and toggles its expansion — it never loads
    videos. Only a double-click requests a load, via :attr:`folder_activated`.
    """

    #: Absolute path of the double-clicked folder; the grid should load it.
    folder_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fs_model = QFileSystemModel(self)
        # Directories only (plus drive roots): the sidebar is a folder
        # navigator, not a file browser. Hidden entries stay hidden.
        self._fs_model.setFilter(
            QDir.Filter.Dirs | QDir.Filter.Drives | QDir.Filter.NoDotAndDotDot
        )
        self.setModel(self._fs_model)
        # Only the name column is meaningful for a folder tree.
        for column in range(1, self._fs_model.columnCount()):
            self.hideColumn(column)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Renaming a folder (double-click edit, F2) must never happen here.
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Double-click loads the folder; Qt's default double-click-expands
        # would fight that, so expansion is handled in _on_clicked instead.
        self.setExpandsOnDoubleClick(False)
        self._root_pending: str | None = None
        self._reveal_chain: list[Path] = []
        self._fs_model.directoryLoaded.connect(self._on_directory_loaded)
        self.clicked.connect(self._on_clicked)
        self.doubleClicked.connect(self._on_double_clicked)

    # -- root + selection ---------------------------------------------------------

    def set_root(self, path: Path) -> None:
        """Restrict the tree to *path* (tests isolate the tree to tmp dirs)."""
        self._root_pending = str(path)
        self._fs_model.setRootPath(str(path))

    def select_path(self, path: Path) -> None:
        """Reveal and select *path*, expanding ancestors as they load.

        ``QFileSystemModel`` populates directories lazily, so ancestors of
        *path* may not exist in the model yet; each ``directoryLoaded``
        emission advances the walk one level. Best effort: if a component
        never materialises (deleted folder, hidden ancestor, absent drive)
        the reveal quietly stops without affecting anything else.
        """
        target = Path(path)
        root_str = self._fs_model.rootPath()
        root = Path(root_str) if root_str else None
        chain: list[Path] = []
        current = target
        while True:
            chain.append(current)
            if root is not None and current == root:
                break  # the view's root index: always visible
            parent = current.parent
            if parent == current:
                break  # filesystem root (a drive): a top-level row already
            current = parent
        chain.reverse()  # outermost ancestor first, the target itself last
        self._reveal_chain = chain
        self._reveal_step()

    def _reveal_step(self) -> None:
        chain = self._reveal_chain
        if not chain:
            return
        candidate = chain[0]
        index = self._fs_model.index(str(candidate))
        if index.isValid():
            chain.pop(0)
            if chain:
                # Loading this level's children unlocks the next candidate.
                self.expand(index)
            else:
                self.setCurrentIndex(index)
                self.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
            return
        parent = candidate.parent
        if parent != candidate:
            parent_index = self._fs_model.index(str(parent))
            if parent_index.isValid():
                self.expand(parent_index)

    def _on_directory_loaded(self, path: str) -> None:
        pending = self._root_pending
        if pending is not None and Path(path) == Path(pending):
            self.setRootIndex(self._fs_model.index(path))
            self._root_pending = None
        self._reveal_step()

    # -- pointer behaviour ---------------------------------------------------------

    def _on_clicked(self, index: QModelIndex) -> None:
        """Single click: select (Qt default) + toggle expansion, never scan."""
        if not index.isValid() or not self._fs_model.isDir(index):
            return
        if self.isExpanded(index):
            self.collapse(index)
        else:
            self.expand(index)

    def _on_double_clicked(self, index: QModelIndex) -> None:
        """Double click: load the folder into the grid, children revealed."""
        if not index.isValid() or not self._fs_model.isDir(index):
            return
        self.expand(index)  # opening a folder always ends with children shown
        self.folder_activated.emit(self._fs_model.filePath(index))