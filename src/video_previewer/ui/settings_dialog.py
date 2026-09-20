"""Tabbed application settings panel."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..cache.cache import ThumbnailCache
from ..open_external import open_externally


def cache_directory_size(directory: Path) -> int:
    """Return the recursive byte size of *directory*, skipping unreadable files."""
    total = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def format_byte_size(size: int) -> str:
    """Format a byte count compactly for the settings panel."""
    value = float(max(0, size))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            break
        value /= 1024.0
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


class _SizeSignals(QObject):
    ready = Signal(int, int)


class _SizeJob(QRunnable):
    def __init__(self, directory: Path, generation: int) -> None:
        super().__init__()
        self._directory = directory
        self._generation = generation
        self.signals = _SizeSignals()

    @Slot()
    def run(self) -> None:
        self.signals.ready.emit(
            cache_directory_size(self._directory), self._generation
        )


class _ClearSignals(QObject):
    finished = Signal(int)
    failed = Signal(str)


class CacheClearJob(QRunnable):
    """Clear the persistent cache away from the GUI thread."""

    def __init__(self, cache: ThumbnailCache) -> None:
        super().__init__()
        self._cache = cache
        self.signals = _ClearSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.finished.emit(self._cache.clear_all())
        except Exception as exc:  # noqa: BLE001 - cache maintenance fails soft
            self.signals.failed.emit(str(exc))


class SettingsDialog(QDialog):
    """Modeless tabbed panel for application-level settings and maintenance."""

    clear_cache_requested = Signal()

    def __init__(self, cache_directory: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cache_directory = cache_directory
        self._size_job: _SizeJob | None = None
        self._size_generation = 0

        self.setWindowTitle(f"{config.APP_NAME} Settings")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        tabs.setObjectName("settingsTabs")
        tabs.addTab(self._build_cache_tab(), "Cache")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

        self.refresh_cache_size()

    def _build_cache_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        location = QLabel(str(self._cache_directory), tab)
        location.setObjectName("cachePathLabel")
        location.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        location.setWordWrap(True)
        layout.addWidget(location)

        self._location_btn = QPushButton("Cache Location", tab)
        self._location_btn.setObjectName("cacheLocationButton")
        self._location_btn.clicked.connect(self._open_cache_location)
        layout.addWidget(self._location_btn)

        self._clear_btn = QPushButton("Clear Cache", tab)
        self._clear_btn.setObjectName("clearCacheButton")
        self._clear_btn.clicked.connect(self.clear_cache_requested)
        layout.addWidget(self._clear_btn)

        self._size_label = QLabel("Cache size: Calculating…", tab)
        self._size_label.setObjectName("cacheSizeLabel")
        layout.addWidget(self._size_label)
        layout.addStretch(1)
        return tab

    @Slot()
    def _open_cache_location(self) -> None:
        try:
            self._cache_directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        open_externally(self._cache_directory)

    def set_cache_busy(self, busy: bool) -> None:
        self._clear_btn.setEnabled(not busy)
        self._clear_btn.setText("Clearing…" if busy else "Clear Cache")
        if busy:
            self._size_label.setText("Cache size: Clearing…")

    def show_cache_error(self) -> None:
        self.set_cache_busy(False)
        self._size_label.setText("Cache size: Unable to clear cache")

    def refresh_cache_size(self) -> None:
        self._size_label.setText("Cache size: Calculating…")
        self._size_generation += 1
        job = _SizeJob(self._cache_directory, self._size_generation)
        self._size_job = job
        job.signals.ready.connect(self._on_size_ready)
        QThreadPool.globalInstance().start(job)

    @Slot(int, int)
    def _on_size_ready(self, size: int, generation: int) -> None:
        if generation != self._size_generation:
            return
        self._size_label.setText(f"Cache size: {format_byte_size(size)}")
        self._size_job = None
