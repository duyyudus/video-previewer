"""Tabbed application settings panel."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSettings, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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

    def __init__(
        self,
        cache_directory: Path,
        parent: QWidget | None = None,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__(parent)
        self._cache_directory = cache_directory
        self._settings = settings if settings is not None else QSettings()
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
        tabs.addTab(self._build_frames_tab(), "Frames")
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

    def _build_frames_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        intro = QLabel(
            "Extract frames (tile right-click menu) writes each video's "
            "frames into a <video name>_frames folder in:",
            tab,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self._frames_label = QLabel(tab)
        self._frames_label.setObjectName("framesPathLabel")
        self._frames_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._frames_label.setWordWrap(True)
        layout.addWidget(self._frames_label)

        choose = QPushButton("Choose Folder…", tab)
        choose.setObjectName("framesChooseButton")
        choose.clicked.connect(self._choose_frames_location)
        layout.addWidget(choose)

        self._frames_reset_btn = QPushButton("Use Video Location", tab)
        self._frames_reset_btn.setObjectName("framesResetButton")
        self._frames_reset_btn.clicked.connect(lambda: self.set_frames_location(None))
        layout.addWidget(self._frames_reset_btn)
        layout.addStretch(1)

        self._refresh_frames_location()
        return tab

    def frames_location(self) -> Path | None:
        """Custom folder for frames folders; None = next to each video."""
        value = self._settings.value(config.SETTING_FRAMES_DIR)
        if isinstance(value, str) and value.strip():
            return Path(value)
        return None

    def set_frames_location(self, folder: Path | None) -> None:
        if folder is None:
            self._settings.remove(config.SETTING_FRAMES_DIR)
        else:
            self._settings.setValue(config.SETTING_FRAMES_DIR, str(folder))
        self._refresh_frames_location()

    def _refresh_frames_location(self) -> None:
        folder = self.frames_location()
        self._frames_label.setText(
            str(folder) if folder is not None
            else "The same folder as the video (default)"
        )
        self._frames_reset_btn.setEnabled(folder is not None)

    @Slot()
    def _choose_frames_location(self) -> None:
        current = self.frames_location()
        folder = QFileDialog.getExistingDirectory(
            self, "Select a folder for extracted frames",
            str(current) if current is not None else "",
        )
        if folder:
            self.set_frames_location(Path(folder))

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
