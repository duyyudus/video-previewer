"""Quick access: the user's pinned folders, shown above the folder tree.

A plain ``QListWidget`` — a handful of rows, so no model/view machinery is
needed. Rows show the full path; a click loads the folder into the grid; right-click unpins; rows can
be dragged to reorder. Pins persist in ``QSettings`` as a JSON list of paths
(a JSON string sidesteps the INI/registry quirk where a one-item list reads
back as a bare string). A pinned folder that no longer exists stays listed,
dimmed and inert, so an unplugged drive does not silently lose its pin.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileIconProvider,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QWidget,
)

from .. import config

_PATH_ROLE = Qt.ItemDataRole.UserRole


def _same_key(path: str) -> str:
    """Comparison form of a folder path (case-insensitive on Windows)."""
    return os.path.normcase(os.path.normpath(path))


class QuickAccessList(QListWidget):
    """Pinned folders; a click emits :attr:`folder_activated`."""

    #: Absolute path of the clicked pin; the grid should load it.
    folder_activated = Signal(str)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._icons = QFileIconProvider()
        self.setUniformItemSizes(True)
        # Rows show full paths (many pins can share a folder name). When the
        # sidebar is too narrow, elide the *start*: the distinguishing part
        # of same-name folders is their parent, right before the name.
        self.setTextElideMode(Qt.TextElideMode.ElideLeft)
        self.setWordWrap(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Reorder by dragging rows; nothing from outside is accepted.
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.model().rowsMoved.connect(self._save)
        self.itemClicked.connect(self._on_item_clicked)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self._load()

    # -- pins ------------------------------------------------------------------

    def pins(self) -> list[str]:
        return [self.item(row).data(_PATH_ROLE) for row in range(self.count())]

    def is_pinned(self, path: str) -> bool:
        key = _same_key(path)
        return any(_same_key(p) == key for p in self.pins())

    def pin(self, path: str) -> None:
        if self.is_pinned(path):
            return
        self._add_item(os.path.normpath(path))
        self._save()

    def unpin(self, path: str) -> None:
        key = _same_key(path)
        for row in reversed(range(self.count())):
            if _same_key(self.item(row).data(_PATH_ROLE)) == key:
                self.takeItem(row)
        self._save()

    def toggle_pin(self, path: str) -> None:
        if self.is_pinned(path):
            self.unpin(path)
        else:
            self.pin(path)

    def refresh(self) -> None:
        """Re-check which pins exist (drives come and go)."""
        for row in range(self.count()):
            self._style_item(self.item(row))

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self.count():
            return
        # Empty: say how to fill it rather than show a blank box.
        painter = QPainter(self.viewport())
        painter.setPen(
            self.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        )
        painter.drawText(
            self.viewport().rect().adjusted(8, 8, -8, -8),
            Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            "Right-click a folder below to pin it here.",
        )
        painter.end()

    # -- items -----------------------------------------------------------------

    def _add_item(self, path: str) -> None:
        item = QListWidgetItem(path)
        item.setData(_PATH_ROLE, path)
        item.setToolTip(path)
        item.setIcon(self._icons.icon(QFileIconProvider.IconType.Folder))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
        self._style_item(item)
        self.addItem(item)

    def _style_item(self, item: QListWidgetItem) -> None:
        path = item.data(_PATH_ROLE)
        if Path(path).is_dir():
            item.setData(Qt.ItemDataRole.ForegroundRole, None)
            item.setToolTip(path)
        else:
            dim = self.palette().color(
                QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text
            )
            item.setForeground(dim)
            item.setToolTip(f"{path}\n(folder not found)")

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self._style_item(item)
        path = item.data(_PATH_ROLE)
        if Path(path).is_dir():
            self.folder_activated.emit(path)

    def _show_context_menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        menu = QMenu(self)
        unpin = menu.addAction("Unpin from Quick access")
        if menu.exec(self.viewport().mapToGlobal(pos)) is unpin:
            self.unpin(item.data(_PATH_ROLE))

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(str(self._settings.value(config.SETTING_QUICK_ACCESS, "[]")))
        except ValueError:
            raw = []  # fail soft: a corrupt value means no pins
        seen: set[str] = set()
        for path in raw if isinstance(raw, list) else []:
            if isinstance(path, str) and path and _same_key(path) not in seen:
                seen.add(_same_key(path))
                self._add_item(path)

    def _save(self, *_args) -> None:
        self._settings.setValue(config.SETTING_QUICK_ACCESS, json.dumps(self.pins()))
