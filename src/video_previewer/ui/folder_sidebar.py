"""Folder-navigation sidebar.

A directory tree (``QTreeView`` over a directories-only ``QFileSystemModel``)
for browsing the filesystem and picking the folder shown in the grid. The
model loads directory contents lazily, so browsing never blocks the GUI
thread, and the tree keeps itself current when folders appear or disappear
on disk. Accepts Move drops of file URLs onto a folder row (see
``files_dropped``). Right-click offers pinning a folder to Quick access
(see ``pin_toggled``).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QDir, QModelIndex, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileSystemModel,
    QMenu,
    QTreeView,
    QWidget,
)

log = logging.getLogger(__name__)

#: Mirror of VideoGrid._DEBUG_POINTER (VIDEO_PREVIEWER_DEBUG_POINTER=1):
#: logs every drag gate decision, which is the quickest way to see what
#: the platform actually proposed (the macOS multi-URL drag session is a
#: known liar about this).
_DEBUG_POINTER = bool(os.environ.get("VIDEO_PREVIEWER_DEBUG_POINTER"))


class FolderSidebar(QTreeView):
    """Directory tree for choosing the folder shown in the grid.

    Clicking a folder selects it and toggles its expansion — it never loads
    videos. Only a double-click requests a load, via :attr:`folder_activated`.
    """

    #: Absolute path of the double-clicked folder; the grid should load it.
    folder_activated = Signal(str)
    #: Local file paths (list of str) dragged from the grid onto a folder
    #: row, and that folder's path (str); the window performs the move.
    files_dropped = Signal(list, str)
    #: Absolute path of a folder whose Quick access pin should flip.
    pin_toggled = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fs_model = QFileSystemModel(self)
        # Directories only (plus drive roots): the sidebar is a folder
        # navigator, not a file browser. Include hidden folders so every
        # location reachable through the OS is also reachable here.
        self._fs_model.setFilter(
            QDir.Filter.Dirs
            | QDir.Filter.Drives
            | QDir.Filter.Hidden
            | QDir.Filter.NoDotAndDotDot
        )
        # Drive roots never populate on their own (see _prime_drive_roots).
        self._prime_drive_roots()
        self.setModel(self._fs_model)
        # Only the name column is meaningful for a folder tree.
        for column in range(1, self._fs_model.columnCount()):
            self.hideColumn(column)
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Renaming a folder (double-click edit, F2) must never happen here.
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Accept videos dragged from the grid onto a folder row (drop-only:
        # folders are never dragged *out* of the tree). The window performs
        # the move; the tree only reports the drop, so the cache/scan stay
        # consistent (see ``files_dropped`` and ``dropEvent``).
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        # Double-click loads the folder; Qt's default double-click-expands
        # would fight that, so expansion is handled in _on_clicked instead.
        self.setExpandsOnDoubleClick(False)
        self._root_pending: str | None = None
        self._reveal_chain: list[Path] = []
        # Row currently under a Move drag: painted as the would-be drop
        # target (see ``drawRow``) so the user can see where the release
        # would land.
        self._drop_row = QModelIndex()
        self._fs_model.directoryLoaded.connect(self._on_directory_loaded)
        self.clicked.connect(self._on_clicked)
        self.doubleClicked.connect(self._on_double_clicked)
        # Asked when the context menu opens, to label Pin vs Unpin; the
        # window points it at the Quick access list.
        self.is_pinned: Callable[[str], bool] = lambda _path: False
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # -- root + selection ---------------------------------------------------------

    def _prime_drive_roots(self) -> None:
        """Force-list every drive root once at construction.

        QFileSystemModel on Windows leaves drive roots permanently empty:
        their background listing never produces rows (observed on Qt 6.11 —
        expanding a drive showed no folders at all, and some drives did not
        even appear at the top level, while a plain QDir listing worked).
        Pointing rootPath at each drive in turn forces that listing; the
        trailing setRootPath("") restores the whole-disk root the tree is
        meant to show.
        """
        for drive in QDir.drives():
            self._fs_model.setRootPath(drive.absolutePath())
        self._fs_model.setRootPath("")

    def set_root(self, path: Path) -> None:
        """Restrict the tree to *path* (tests isolate the tree to tmp dirs)."""
        self._root_pending = str(path)
        self._fs_model.setRootPath(str(path))

    def select_path(self, path: Path) -> None:
        """Reveal and select *path*, expanding ancestors as they load.

        ``QFileSystemModel`` populates directories lazily, so ancestors of
        *path* may not exist in the model yet; each ``directoryLoaded``
        emission advances the walk one level. Best effort: if a component
        never materialises (deleted folder or absent drive), the reveal
        quietly stops without affecting anything else.
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
        # Walk down every level that is already in the model: an ancestor
        # that was expanded (loaded) earlier never emits ``directoryLoaded``
        # again, so waiting for one there would stall the reveal.
        while chain:
            candidate = chain[0]
            index = self._fs_model.index(str(candidate))
            if not index.isValid():
                break
            chain.pop(0)
            if chain:
                # Loading this level's children unlocks the next candidate.
                self.expand(index)
            else:
                self.setCurrentIndex(index)
                self.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
                return
        if not chain:
            return
        candidate = chain[0]
        parent = candidate.parent
        if parent != candidate:
            parent_index = self._fs_model.index(str(parent))
            if parent_index.isValid():
                self.expand(parent_index)
                # Already expanded: expand() is a no-op, so ask for the
                # listing directly (its directoryLoaded resumes the walk).
                if self._fs_model.canFetchMore(parent_index):
                    self._fs_model.fetchMore(parent_index)

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

    def _show_context_menu(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid() or not self._fs_model.isDir(index):
            return
        path = self._fs_model.filePath(index)
        menu = QMenu(self)
        label = "Unpin from Quick access" if self.is_pinned(path) else "Pin to Quick access"
        action = menu.addAction(label)
        if menu.exec(self.viewport().mapToGlobal(pos)) is action:
            self.pin_toggled.emit(path)

    # -- drops from the grid -----------------------------------------------------------

    def _drop_hit(self, event) -> tuple[str | None, QModelIndex]:
        """The (folder path, row) the drag points at; ``(None, invalid)``
        when the drop must be refused.

        Accepts moves of local file URLs over a directory row — the
        sidebar's only supported operation is moving files in (grid drags
        always intend one; an external drag must propose Move explicitly,
        so a would-be copy is refused rather than silently eating the
        source). Refusing everything else (empty tree space, a
        non-directory row, a non-file drag) is equally deliberate: an
        accidental release must never move files somewhere the user did
        not aim at.
        """
        if event.proposedAction() != Qt.DropAction.MoveAction:
            # Belt and braces: the grid's own drags should always propose
            # Move, but the platform re-synthesizes events for the native
            # multi-URL drag session, so tolerate a Copy proposal from an
            # in-app source as long as Move is possible. An external drag
            # must still propose Move explicitly, so a would-be copy never
            # silently eats the source files.
            if event.source() is None:
                return None, QModelIndex()
            if not event.possibleActions() & Qt.DropAction.MoveAction:
                return None, QModelIndex()
        mime = event.mimeData()
        if not mime.hasUrls() or not any(u.isLocalFile() for u in mime.urls()):
            return None, QModelIndex()
        index = self.indexAt(event.position().toPoint())
        if not index.isValid() or not self._fs_model.isDir(index):
            return None, QModelIndex()
        return self._fs_model.filePath(index), index

    def _set_drop_row(self, index: QModelIndex) -> None:
        if index == self._drop_row:
            return
        self._drop_row = index
        # repaint(), not update(): multi-URL drags run inside a native
        # NSDraggingSession whose run-loop mode can defer windowed
        # repaints; a synchronous paint is the one that lands while the
        # session is still alive.
        self.viewport().repaint()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        folder, index = self._drop_hit(event)
        self._set_drop_row(index if folder is not None else QModelIndex())
        if _DEBUG_POINTER:
            # %s only: PySide6's DropAction enum is not int()-convertible,
            # and an exception in this handler silently kills the whole
            # drop gate (the rest of the override never runs).
            log.info(
                "drop enter: pos=%s proposed=%s possible=%s source=%s "
                "urls=%s -> %s",
                event.position().toPoint(),
                event.proposedAction(),
                event.possibleActions(),
                type(event.source()).__name__,
                len(event.mimeData().urls()) if event.mimeData().hasUrls() else 0,
                folder or "refused",
            )
        if folder is not None:
            self._accept_move(event)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        folder, index = self._drop_hit(event)
        if folder is not None:
            self._set_drop_row(index)
            self._accept_move(event)
            return
        # Miss inside the tree. Multi-URL drags run through a native
        # NSDraggingSession whose move events can carry a stale position
        # (the same phantom-move quirk the grid fights), so a missed move
        # must not punish the user: keep the last armed target highlighted
        # and keep offering the drop. The drop event re-validates against
        # the real pointer position, so releasing over empty space still
        # moves nothing.
        if self._drop_row.isValid():
            self._accept_move(event)
        else:
            event.ignore()

    def _accept_move(self, event) -> None:
        # Accept *as a move* explicitly rather than via
        # ``acceptProposedAction``: what the platform proposes for the grid's
        # own drags is unreliable (see ``_drop_hit``), and the sidebar's only
        # operation is a move.
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._set_drop_row(QModelIndex())
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        folder, _index = self._drop_hit(event)
        self._set_drop_row(QModelIndex())
        if folder is None:
            event.ignore()
            return
        paths = [
            u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()
        ]
        self._accept_move(event)
        # Deliberately no super(): QTreeView would forward to QFileSystemModel,
        # which moves the files natively and bypasses the cache/scan migration,
        # leaving a dead tile and a stranded thumbnail behind.
        self.files_dropped.emit(paths, folder)

    def drawRow(self, painter, option, index, rect=None) -> None:  # noqa: N802
        # Qt's paint loop calls the 3-argument overload (PySide dispatches
        # that one here), so accept both shapes and delegate accordingly.
        if rect is None:
            super().drawRow(painter, option, index)
            rect = self.visualRect(index)
        else:
            super().drawRow(painter, option, index, rect)
        if index.isValid() and index == self._drop_row:
            # Explorer's "release here" cue: a tinted box on the folder row
            # a Move drag is currently over.
            accent = self.palette().color(QPalette.ColorRole.Highlight)
            fill = QColor(accent)
            fill.setAlpha(70)
            painter.save()
            painter.setPen(QPen(accent, 1.5))
            painter.setBrush(fill)
            painter.drawRoundedRect(
                QRectF(rect).adjusted(1.5, 1.5, -1.5, -1.5), 3, 3
            )
            painter.restore()
