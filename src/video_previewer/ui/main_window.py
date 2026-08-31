"""Main application window.

Owns folder selection, the grid, and application state. All media work
(scanning, probing, thumbnailing, playback) is delegated to workers and
the shared player; this class only handles UI state.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths, Qt, QThreadPool
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..cache.cache import ThumbnailCache
from ..cache.database import Database
from ..media.player import PreviewPlayer
from ..models.video_item import VideoItem
from ..models.video_model import VideoModel
from ..ui.video_grid import VideoGrid
from ..workers.scanner import ScanResult, ScanSignals, Scanner
from ..workers.thumbnail_worker import ThumbnailQueue

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(config.APP_NAME)
        self.resize(1280, 800)

        # --- persistence + cache -------------------------------------------------
        self._settings = QSettings()
        self._db = Database(config.database_path())
        self._cache = ThumbnailCache(self._db)

        # --- model / view / player -------------------------------------------------
        self._model = VideoModel(self)
        self._grid = VideoGrid(self._model, self)
        self._player = PreviewPlayer(self._grid.viewport(), self)
        self._grid.set_player(self._player)

        self._thumbs = ThumbnailQueue(self._db, self._cache, self)
        self._scan_signals = ScanSignals(self)
        self._scan_gen = 0
        self._scan_signals.items.connect(self._on_scan_items)
        self._scan_signals.finished.connect(self._on_scan_finished)
        self._scan_signals.error.connect(self._on_scan_error)
        self._thumbs.signals.ready.connect(self._on_thumb_ready)
        self._thumbs.signals.failed.connect(self._on_thumb_failed)

        # --- layout -----------------------------------------------------------------
        self._current_folder: Path | None = None
        central = QWidget(self)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(10, 8, 10, 10)
        vbox.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self._browse_btn = QPushButton("Open Folder\u2026", self)
        self._browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._browse_btn.clicked.connect(self._browse)
        self._folder_label = QLabel("No folder selected", self)
        self._folder_label.setToolTip("Currently scanned folder")
        self._recursive_chk = QCheckBox("Subfolders", self)
        self._recursive_chk.toggled.connect(self._on_recursive_toggled)
        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet("color: #9a9aa5;")
        bar.addWidget(self._browse_btn)
        bar.addWidget(self._folder_label, 0, Qt.AlignmentFlag.AlignVCenter)
        bar.addStretch(1)
        bar.addWidget(self._recursive_chk)
        bar.addWidget(self._status_label, 0, Qt.AlignmentFlag.AlignVCenter)

        vbox.addLayout(bar)
        vbox.addWidget(self._grid, 1)
        self.setCentralWidget(central)

        if not config.ffmpeg_path() or not config.ffprobe_path():
            self._status_label.setText("ffmpeg/ffprobe not found \u2014 thumbnails unavailable")

        # Restore the last opened folder (if it still exists).
        last = self._settings.value(config.SETTING_LAST_FOLDER)
        if last:
            candidate = Path(str(last))
            if candidate.is_dir():
                self._recursive_chk.setChecked(
                    bool(self._settings.value(config.SETTING_RECURSIVE, False, type=bool))
                )
                self._open_folder(candidate)

    # -- folder selection ------------------------------------------------------------

    def _browse(self) -> None:
        start = str(self._current_folder or
                    QStandardPaths.writableLocation(QStandardPaths.HomeLocation))
        folder = QFileDialog.getExistingDirectory(
            self, "Select a folder with videos", start
        )
        if folder:
            self._open_folder(Path(folder))

    def _open_folder(self, folder: Path) -> None:
        self._scan_gen += 1  # invalidate any in-flight scan of another folder
        self._current_folder = folder
        self._folder_label.setText(str(folder))
        self._folder_label.setToolTip(str(folder))
        self._settings.setValue(config.SETTING_LAST_FOLDER, str(folder))
        self._settings.setValue(config.SETTING_RECURSIVE, self._recursive_chk.isChecked())

        self._player.leave()
        self._model.clear()
        self._grid.delegate().clear_pixmap_cache()
        self._update_status()

        scanner = Scanner(
            folder, self._recursive_chk.isChecked(), self._db, self._cache,
            self._scan_signals, self._scan_gen,
        )
        QThreadPool.globalInstance().start(scanner)
        log.info("scanning %s (recursive=%s)", folder, self._recursive_chk.isChecked())

    def _on_recursive_toggled(self, checked: bool) -> None:
        self._settings.setValue(config.SETTING_RECURSIVE, checked)
        if self._current_folder is not None:
            self._open_folder(self._current_folder)

    # -- scanner results (main thread, queued from worker) -----------------------------

    def _on_scan_items(self, items: list[VideoItem], gen: int) -> None:
        if gen != self._scan_gen:
            return  # a newer folder scan is in progress; drop stale results
        to_add: list[VideoItem] = []
        to_update: list[tuple[int, VideoItem]] = []
        for item in items:
            row = self._model.row_for(item.path)
            if row is None:
                to_add.append(item)
            else:
                current = self._model.item_at(row)
                if current is not None and current.vid != item.vid:
                    # file changed (size/mtime) -> refresh metadata + thumbnail
                    to_update.append((row, item))
        if to_add:
            # One batched insertion per signal keeps the view cheap even
            # with thousands of items.
            rows = self._model.add_items(to_add)
            for r in rows:
                self._thumbs.request(self._model.item_at(r))
        for row, item in to_update:
            self._model.update_item(row, item)
            self._thumbs.request(item)
        self._update_status()

    def _on_scan_finished(self, result: ScanResult, gen: int) -> None:
        if gen != self._scan_gen:
            return
        removed = self._cache.purge_folder(result.folder, result.fresh)
        if removed:
            log.info("purged %d stale cache entries", removed)
        self._update_status()

    def _on_scan_error(self, message: str) -> None:
        self._status_label.setText(f"Scan failed: {message}")

    def _update_status(self) -> None:
        n = self._model.count()
        self._status_label.setText(f"{n} video{'s' if n != 1 else ''}")

    # -- thumbnail results --------------------------------------------------------------

    def _on_thumb_ready(self, item: VideoItem) -> None:
        row = self._model.row_for(item.path)
        if row is None:
            return
        current = self._model.item_at(row)
        if current is None or current.vid != item.vid:
            return  # folder moved on underneath us; ignore stale result
        self._model.update_item(row, item)

    def _on_thumb_failed(self, path: str) -> None:
        log.warning("thumbnail failed for %s", path)

    # -- shutdown ------------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._player.leave()
        # Let in-flight worker jobs (probing/thumbnailing) drain so they do
        # not race the database close. Bounded so closing stays snappy; the
        # database fails soft for anything that still lingers.
        deadline = time.time() + 5.0
        while self._thumbs.pending_count() > 0 and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        self._db.close()
        super().closeEvent(event)

    # -- test helpers ----------------------------------------------------------------------

    @property
    def model(self) -> VideoModel:
        return self._model

    @property
    def grid(self) -> VideoGrid:
        return self._grid

    @property
    def player(self) -> PreviewPlayer:
        return self._player
