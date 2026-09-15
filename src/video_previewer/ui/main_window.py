"""Main application window.

Owns folder selection, the grid, and application state. All media work
(scanning, probing, thumbnailing, playback) is delegated to workers and
the shared player; this class only handles UI state.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
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
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..cache.cache import ThumbnailCache
from ..cache.database import open_database
from ..media.player import PreviewPlayer
from ..models.video_item import VideoItem
from ..models.video_model import VideoModel
from ..ui import exit_dialog
from ..ui.folder_sidebar import FolderSidebar
from ..ui.video_grid import VideoGrid
from ..workers.scanner import ScanResult, ScanSignals, Scanner
from ..workers.thumbnail_worker import ThumbnailQueue

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(config.APP_NAME)
        self.resize(config.DEFAULT_WINDOW_WIDTH, config.DEFAULT_WINDOW_HEIGHT)

        # --- persistence + cache -------------------------------------------------
        self._settings = QSettings()
        # Fail soft (rule 8): a corrupt metadata DB or broken cache dir must
        # never kill startup; open_database quarantines/retries and falls
        # back to an empty in-memory cache, ThumbnailCache degrades to "no
        # thumbnails" if its directory is unwritable.
        self._db = open_database(config.database_path())
        self._cache = ThumbnailCache(self._db)

        # --- model / view / player -------------------------------------------------
        self._model = VideoModel(self)
        self._grid = VideoGrid(self._model, self)
        self._player = PreviewPlayer(self._grid.viewport(), self)
        self._grid.set_player(self._player)
        # Double-clicking empty grid space (no video under the pointer) is a
        # shortcut for the "Open Folder…" picker.
        self._grid.open_folder_requested.connect(self._browse)
        # The sidebar tree is created here too: its double-click loads a
        # folder into the grid through the same funnel as the picker.
        self._sidebar = FolderSidebar(self)
        self._sidebar.folder_activated.connect(self._on_sidebar_folder_activated)

        self._thumbs = ThumbnailQueue(self._db, self._cache, self)
        self._scan_signals = ScanSignals(self)
        self._scan_gen = 0
        # Set at the start of closeEvent: while the window is closing, the
        # drain loop still runs the event queue, and mutations coming in
        # through queued events (clicks, late scan results) must be no-ops.
        self._closing = False
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
        self._sidebar_btn = QPushButton("Sidebar", self)
        self._sidebar_btn.setCheckable(True)
        self._sidebar_btn.toggled.connect(self._on_sidebar_toggled)
        self._folder_label = QLabel("No folder selected", self)
        self._folder_label.setToolTip("Currently scanned folder")
        self._recursive_chk = QCheckBox("Subfolders", self)
        self._recursive_chk.toggled.connect(self._on_recursive_toggled)
        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet("color: #9a9aa5;")
        bar.addWidget(self._browse_btn)
        bar.addWidget(self._sidebar_btn)
        bar.addWidget(self._folder_label, 0, Qt.AlignmentFlag.AlignVCenter)
        bar.addStretch(1)
        bar.addWidget(self._recursive_chk)
        bar.addWidget(self._status_label, 0, Qt.AlignmentFlag.AlignVCenter)

        vbox.addLayout(bar)
        # Sidebar + grid share the content area: the grid absorbs any extra
        # width, and dragging the handle can never collapse the tree to zero
        # (hiding it is the toggle button's job, not the splitter's).
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setChildrenCollapsible(False)
        self._splitter.addWidget(self._sidebar)
        self._splitter.addWidget(self._grid)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        vbox.addWidget(self._splitter, 1)
        self.setCentralWidget(central)

        unavailable: list[str] = []
        if not config.ffmpeg_path() or not config.ffprobe_path():
            unavailable.append("ffmpeg/ffprobe not found")
        if not self._cache.usable:
            unavailable.append(f"cannot write {self._cache.directory}")
        if unavailable:
            self._status_label.setText(
                "; ".join(unavailable) + " \u2014 thumbnails unavailable"
            )

        # Restore the remembered window geometry (size, position, maximized
        # state). A missing or invalid blob leaves the default size above.
        geometry = self._settings.value(config.SETTING_WINDOW_GEOMETRY)
        if geometry:
            self.restoreGeometry(geometry)

        # Sidebar visibility and split sizes. restoreState ignores a corrupt
        # or mismatched blob (fail soft), leaving the configured default.
        visible = self._settings.value(config.SETTING_SIDEBAR_VISIBLE, True, type=bool)
        # setVisible directly: setChecked only fires `toggled` on a state
        # *change*, so a persisted "hidden" (the button's default) would
        # never reach the sidebar otherwise.
        self._sidebar_btn.setChecked(visible)
        self._sidebar.setVisible(visible)
        state = self._settings.value(config.SETTING_SIDEBAR_SPLITTER)
        if state:
            self._splitter.restoreState(state)
        else:
            # First run: sidebar at its configured width; the grid's stretch
            # factor soaks up everything else.
            self._splitter.setSizes([config.SIDEBAR_WIDTH, 1])

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
        if self._closing:
            return
        start = str(self._current_folder or
                    QStandardPaths.writableLocation(QStandardPaths.HomeLocation))
        folder = QFileDialog.getExistingDirectory(
            self, "Select a folder with videos", start
        )
        if folder:
            self._open_folder(Path(folder))

    def _open_folder(self, folder: Path) -> None:
        if self._closing:
            return
        self._scan_gen += 1  # invalidate any in-flight scan of another folder
        gen = self._scan_gen
        self._current_folder = folder
        self._folder_label.setText(str(folder))
        self._folder_label.setToolTip(str(folder))
        self._sidebar.select_path(folder)  # reveal it in the tree
        self._settings.setValue(config.SETTING_LAST_FOLDER, str(folder))
        self._settings.setValue(config.SETTING_RECURSIVE, self._recursive_chk.isChecked())

        self._player.leave()
        self._grid.clear_hover()  # model resets; cursor tile must not stay "hovered"
        self._model.clear()
        self._thumbs.clear_pending()  # abandoned folder's queued jobs never start
        self._grid.delegate().clear_pixmap_cache()
        self._update_status()

        scanner = Scanner(
            folder, self._recursive_chk.isChecked(), self._db, self._cache,
            self._scan_signals, gen,
            is_stale=self._scan_stale(gen),
        )
        QThreadPool.globalInstance().start(scanner)
        log.info("scanning %s (recursive=%s)", folder, self._recursive_chk.isChecked())

    def _scan_stale(self, generation: int) -> Callable[[], bool]:
        """Cancellation hook polled by a scan of *generation*'s worker thread.

        The scan is obsolete once a newer one has taken over *or* the window
        starts closing (otherwise it outlives the database it writes to and
        keeps stat()-ing the tree during teardown). Plain attribute reads
        only: no Qt state is touched off the GUI thread.
        """
        return lambda: generation != self._scan_gen or self._closing

    def _on_recursive_toggled(self, checked: bool) -> None:
        self._settings.setValue(config.SETTING_RECURSIVE, checked)
        if self._current_folder is not None:
            self._open_folder(self._current_folder)

    # -- sidebar -----------------------------------------------------------------------

    def _on_sidebar_toggled(self, visible: bool) -> None:
        self._sidebar.setVisible(visible)
        self._settings.setValue(config.SETTING_SIDEBAR_VISIBLE, visible)

    def _on_sidebar_folder_activated(self, path: str) -> None:
        """Load the double-clicked sidebar folder through the shared funnel."""
        self._open_folder(Path(path))

    # -- scanner results (main thread, queued from worker) -----------------------------

    def _on_scan_items(self, items: list[VideoItem], gen: int) -> None:
        if gen != self._scan_gen or self._closing:
            return  # stale scan, or the window is draining for close
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
        if gen != self._scan_gen or self._closing:
            return
        removed = self._cache.purge_folder(result.folder, result.fresh)
        if removed:
            log.info("purged %d stale cache entries", removed)
        self._update_status()

    def _on_scan_error(self, message: str) -> None:
        if self._closing:
            return  # a dying scan's error is not something the user can act on
        self._status_label.setText(f"Scan failed: {message}")

    def _update_status(self) -> None:
        n = self._model.count()
        self._status_label.setText(f"{n} video{'s' if n != 1 else ''}")

    # -- thumbnail results --------------------------------------------------------------

    def _on_thumb_ready(self, item: VideoItem) -> None:
        if self._closing:
            return  # no model mutation while the window drains for close
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
        # Re-entrancy guard: the drain loop below keeps processing the event
        # queue, so from here on every mutation reachable through a queued
        # event (_browse, _open_folder, late scan results/errors, thumb
        # updates, and every pointer gesture in the grid) is a no-op. Nothing
        # can start new workers or change the model mid-close.
        self._closing = True
        # Remember the window geometry + sidebar split no matter what the exit
        # dialog decides below; sync now so it is on disk even if teardown
        # hits a snag.
        self._settings.setValue(config.SETTING_WINDOW_GEOMETRY, self.saveGeometry())
        self._settings.setValue(
            config.SETTING_SIDEBAR_SPLITTER, self._splitter.saveState()
        )
        self._settings.sync()
        folder = self._current_folder
        # Ask before draining so the user is not kept waiting for workers.
        keep = folder is None or exit_dialog.ask_keep_on_exit(
            self, folder, self._model.count()
        )
        self._player.leave()
        # The window is still on screen while we pump events: make the grid
        # stop answering the pointer so a stray hover cannot restart the
        # player we just stopped, nor a press pair launch an external player.
        self._grid.set_closing()
        # Let in-flight worker jobs (probing/thumbnailing) drain so they do
        # not race the database close. Bounded so closing stays snappy; the
        # database fails soft for anything that still lingers. processEvents
        # is safe here because _closing turns every queued mutation into a
        # no-op; the sleep only caps the spin between event pumps.
        deadline = time.time() + 5.0
        while self._thumbs.pending_count() > 0 and time.time() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.02)
        # Resolve the keep/discard decision after the drain: no workers are
        # still writing cache rows when we purge.
        if folder is not None:
            if keep:
                # Re-assert + sync so the choice is on disk, not just in this
                # QSettings instance's buffer.
                self._settings.setValue(config.SETTING_LAST_FOLDER, str(folder))
                self._settings.setValue(
                    config.SETTING_RECURSIVE, self._recursive_chk.isChecked()
                )
                self._settings.sync()
            else:
                self._forget_folder(folder)
        self._db.close()
        super().closeEvent(event)

    def _forget_folder(self, folder: Path) -> None:
        """Discard the selected folder: forget the setting + drop its cache."""
        self._settings.remove(config.SETTING_LAST_FOLDER)
        self._settings.remove(config.SETTING_RECURSIVE)
        self._settings.sync()
        removed = self._cache.purge_folder_all(folder)
        if removed:
            log.info("discarded %d cached video(s) for %s", removed, folder)

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

    @property
    def sidebar(self) -> FolderSidebar:
        return self._sidebar
