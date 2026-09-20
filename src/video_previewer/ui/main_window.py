"""Main application window.

Owns folder selection, the grid, and application state. All media work
(scanning, probing, thumbnailing, playback) is delegated to workers and
the shared player; this class only handles UI state.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from PySide6.QtCore import QCoreApplication, QSettings, QStandardPaths, Qt, QThreadPool
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..cache.cache import ThumbnailCache
from ..cache.database import open_database
from ..file_deletion import move_to_trash
from ..media.player import PreviewPlayer
from ..models.sorting import SortKey, SortOrder
from ..models.video_item import VideoItem, normalize_path
from ..models.video_model import VideoModel
from ..ui import delete_dialog, exit_dialog, rename_dialog
from ..ui.folder_sidebar import FolderSidebar
from ..ui.video_grid import VideoGrid
from ..workers.scanner import ScanResult, ScanSignals, Scanner
from ..workers.thumbnail_worker import ThumbnailQueue

log = logging.getLogger(__name__)

_E = TypeVar("_E", bound=StrEnum)


def _saved_enum(raw: object, enum: type[_E], default: _E) -> _E:
    """Read a persisted enum setting back (fail soft: junk means *default*)."""
    try:
        return enum(str(raw))
    except ValueError:
        return default


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
        # F2 on a selected tile renames the video file in place.
        self._grid.rename_requested.connect(self._rename_selected)
        # Delete moves the selection to Trash; Shift+Delete permanently
        # deletes it after an explicit confirmation.
        self._grid.delete_requested.connect(self._delete_selected)
        # The sidebar tree is created here too: its double-click loads a
        # folder into the grid through the same funnel as the picker.
        self._sidebar = FolderSidebar(self)
        self._sidebar.folder_activated.connect(self._on_sidebar_folder_activated)
        self._rescan_action = QAction("Rescan Folder", self)
        self._rescan_action.setShortcut(QKeySequence("F5"))
        self._rescan_action.triggered.connect(self._rescan_current_folder)
        self.addAction(self._rescan_action)
        # Drag-to-move: a drop on a sidebar folder moves the files here
        # (cache + scan migrate); a drag that ends elsewhere and executes a
        # Move means a file manager moved the files out on disk, so the grid
        # must drop those tiles. The ``_drop_handled_internally`` latch tells
        # the two cases apart (a sidebar drop lands before ``drag_finished``).
        self._sidebar.files_dropped.connect(self._on_files_dropped)
        self._grid.drag_started.connect(self._on_drag_started)
        self._grid.drag_finished.connect(self._on_drag_finished)
        self._drop_handled_internally = False

        self._thumbs = ThumbnailQueue(self._db, self._cache, self)
        self._scan_signals = ScanSignals(self)
        self._scan_gen = 0
        # Renames done since the current folder was opened:
        # normalized old path -> new path. A scan's fresh-file list is
        # captured while walking, but its purge runs when it finishes, so a
        # rename landing in between would otherwise make the purge treat the
        # renamed file as deleted (dropping the migrated row + JPEG) and a
        # late batch would re-add the dead old path as a ghost tile. Scan
        # results are therefore remapped through this map.
        self._renamed: dict[str, str] = {}
        # Successful deletions in the current folder session. A scanner or
        # thumbnail job may already hold the old file in a worker result;
        # these tombstones stop those late results resurrecting tiles/cache.
        self._removed: set[str] = set()
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
        self._sort_btn = QPushButton(self)
        self._sort_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._sort_btn.setToolTip("Sort the thumbnails by name or modification date")
        self._sort_actions: dict[SortKey | SortOrder, QAction] = {}
        self._sort_btn.setMenu(self._build_sort_menu())
        self._status_label = QLabel("", self)
        self._status_label.setStyleSheet("color: #9a9aa5;")
        bar.addWidget(self._browse_btn)
        bar.addWidget(self._sidebar_btn)
        bar.addWidget(self._folder_label, 0, Qt.AlignmentFlag.AlignVCenter)
        bar.addStretch(1)
        bar.addWidget(self._recursive_chk)
        bar.addWidget(self._sort_btn)
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

        # Restore the remembered sort *before* the folder: the first scan
        # batch should already land in the right order.
        self._apply_sort(
            _saved_enum(
                self._settings.value(config.SETTING_SORT_KEY), SortKey, SortKey.NAME
            ),
            _saved_enum(
                self._settings.value(config.SETTING_SORT_ORDER),
                SortOrder,
                SortOrder.ASC,
            ),
        )

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
        self._renamed.clear()  # renames belong to the folder session
        self._removed.clear()  # deletions belong to the folder session
        self._current_folder = folder
        self._folder_label.setText(str(folder))
        self._folder_label.setToolTip(str(folder))
        self._sidebar.select_path(folder)  # reveal it in the tree
        self._settings.setValue(config.SETTING_LAST_FOLDER, str(folder))
        self._settings.setValue(config.SETTING_RECURSIVE, self._recursive_chk.isChecked())

        self._player.leave()
        self._grid.clear_hover()  # model resets; cursor tile must not stay "hovered"
        self._model.clear()
        # The scan arrives in small batches; hold reorders back until it ends
        # so a big folder costs one relayout instead of one per batch.
        self._model.set_layout_deferred(True)
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

    # -- sorting ---------------------------------------------------------------

    def _build_sort_menu(self) -> QMenu:
        """Sort-by choices, then direction; one checkable action per option.

        Two exclusive groups (key / order) rather than one entry per
        key+direction pair: the menu stays two short lists however many
        options exist, and Qt maintains the checked state for us.
        """
        menu = QMenu(self._sort_btn)
        self._add_sort_options(menu, SortKey)
        menu.addSeparator()
        self._add_sort_options(menu, SortOrder)
        return menu

    def _add_sort_options(self, menu: QMenu, options: type[StrEnum]) -> None:
        group = QActionGroup(self)
        group.setExclusive(True)
        for option in options:
            action = menu.addAction(option.label)
            action.setCheckable(True)
            action.triggered.connect(self._on_sort_selected)
            group.addAction(action)
            self._sort_actions[option] = action

    def _on_sort_selected(self) -> None:
        """A menu entry was clicked: push the (already checked) pair into effect."""
        self._apply_sort(
            next(key for key in SortKey if self._sort_actions[key].isChecked()),
            next(o for o in SortOrder if self._sort_actions[o].isChecked()),
        )

    def _apply_sort(self, key: SortKey, order: SortOrder) -> None:
        """Make *key*/*order* the sort: model, menu, button label, settings."""
        self._sort_actions[key].setChecked(True)
        self._sort_actions[order].setChecked(True)
        self._sort_btn.setText(f"Sort: {key.label} {order.arrow}")
        self._model.set_sort(key, order)
        self._settings.setValue(config.SETTING_SORT_KEY, str(key))
        self._settings.setValue(config.SETTING_SORT_ORDER, str(order))

    # -- sidebar -----------------------------------------------------------------------

    def _on_sidebar_toggled(self, visible: bool) -> None:
        self._sidebar.setVisible(visible)
        self._settings.setValue(config.SETTING_SIDEBAR_VISIBLE, visible)

    def _on_sidebar_folder_activated(self, path: str) -> None:
        """Load the double-clicked sidebar folder through the shared funnel."""
        self._open_folder(Path(path))

    def _rescan_current_folder(self) -> None:
        """F5: reload the current folder just like activating it in the sidebar."""
        if self._current_folder is not None:
            self._open_folder(self._current_folder)

    # -- drag to move ---------------------------------------------------------------

    def _on_drag_started(self, paths: list) -> None:
        # New gesture: forget whether the *previous* drag ended internally;
        # only a sidebar drop during *this* drag counts.
        self._drop_handled_internally = False

    def _on_files_dropped(self, paths: list, folder: str) -> None:
        """A sidebar drop: move the dropped files into the target folder."""
        self._drop_handled_internally = True
        self._move_videos([Path(p) for p in paths], Path(folder))

    def _on_drag_finished(self, paths: list, action: Qt.DropAction) -> None:
        if self._closing or self._drop_handled_internally:
            return
        if action == Qt.DropAction.MoveAction:
            self._finish_source_move(paths)
        # The drop left the app: a file manager copied or moved the files
        # itself. The executed action is a poor signal across platforms
        # (Finder defaults drags to copy, and even a move can land back as
        # something else), so reconcile against the disk instead: a dragged
        # file that no longer exists was moved out, and its tile must go
        # now, not on the next scan. Its scan-cache entry goes too, or the
        # cache replay would resurrect the tile as a ghost on reopen.
        gone = [Path(p) for p in paths if not Path(p).exists()]
        if gone:
            self._model.remove_items(gone)
            self._db.remove_scan_entries([p.as_posix() for p in gone])
            self._grid.clear_hover()
            self._update_status()

    def _finish_source_move(self, paths: list) -> None:
        """Delete originals after a drag target accepted a source-side move.

        Qt's drag contract makes the source responsible for removing its data
        when ``QDrag.exec()`` returns ``MoveAction``. Explorer can implement
        that by copying the files to the destination and returning the move
        result, leaving this source to finish the operation. A plain
        ``CopyAction`` never lands here. Neither does Windows-only
        ``TargetMoveAction``: that value explicitly transfers ownership to
        the target and tells the source *not* to delete its data.

        The tiles themselves go in the reconciliation that follows, which
        sees the files are gone.
        """
        for p in paths:
            src = Path(p)
            try:
                src.unlink()
            except FileNotFoundError:
                continue  # an optimized move after all
            except OSError as exc:
                log.warning(
                    "could not delete %s after a drag-out move: %s",
                    src,
                    exc.strerror or exc,
                )

    def _move_videos(self, paths: list[Path], target: Path) -> None:
        """Move the given files into *target*, migrating caches and rows.

        Mirrors :meth:`_rename_selected` for a batch across folders: every
        move changes the file's cache identity (``vid`` hashes the path), so
        the cached row + JPEG are migrated (rule 4), the scan cache is
        patched, the in-flight-scan remap is recorded, and the model row is
        replaced in place (its resort moves the tile; the persistent-index
        remap carries the selection along). ``shutil.move`` rather than
        ``Path.rename`` because the target may live on another volume.
        """
        if self._closing or not target.is_dir():
            return
        self._player.leave()
        self._grid.clear_hover()
        failed: list[str] = []
        moved = 0
        for src in paths:
            row = self._model.row_for(src)
            if row is None:
                continue  # not one of ours (external drag or already moved)
            item = self._model.item_at(row)
            if item is None:
                continue
            new_path = target / item.filename
            if normalize_path(new_path) == normalize_path(item.path):
                continue  # dropped onto its own folder: nothing to do
            if new_path.exists():
                failed.append(f"{item.filename} (target already exists)")
                continue
            try:
                shutil.move(str(item.path), str(new_path))
            except OSError as exc:
                failed.append(f"{item.filename}: {exc.strerror or exc}")
                continue
            moved += 1
            st = new_path.stat()
            new_item = VideoItem(new_path, st.st_size, st.st_mtime)
            thumb = self._cache.rename(item, new_item)
            if thumb is not None:
                new_item = new_item.with_thumbnail(thumb)
            stays = self._stays_in_grid(new_path)
            if stays:
                self._db.rename_scan_entry(item.path.as_posix(), new_path.as_posix())
            else:
                # Leaving the folder for good: no scan cache may keep
                # replaying the old path as a ghost tile. (The destination
                # folder picks the file up on its next real walk.)
                self._db.remove_scan_entries([item.path.as_posix()])
            self._note_moved(item.path, new_path)
            # Re-resolve right before the mutation (the contract: a row is
            # only valid immediately after ``row_for``).
            row = self._model.row_for(src)
            if row is None:
                continue  # the folder switched under us; file is already moved
            if stays:
                self._model.rename_item(row, new_item)
                if not new_item.thumb_ready:
                    self._thumbs.request(new_item)
            else:
                # The file left the shown folder: the tile goes with it.
                self._model.remove_items([src])
                self._grid.clear_hover()
        if moved:
            log.info("moved %d video(s) into %s", moved, target)
        if failed:
            QMessageBox.warning(
                self,
                config.APP_NAME,
                "Could not move:\n" + "\n".join(failed),
            )
        self._update_status()

    def _stays_in_grid(self, new_path: Path) -> bool:
        """Whether a file at *new_path* still belongs to the folder shown.

        Mirrors the scan's scope: flat scans cover the folder itself,
        recursive ones its whole subtree. A file moved elsewhere drops its
        tile instead of lingering as a claim that the folder still holds it.
        """
        folder = self._current_folder
        if folder is None:
            return False
        if self._recursive_chk.isChecked():
            return folder in new_path.parents
        return new_path.parent == folder

    def _note_moved(self, old_path: Path, new_path: Path) -> None:
        """Record an old -> new path hop for in-flight scan reconciliation.

        A scan's fresh-file list is captured while walking but its purge runs
        when it finishes, so a mutation landing in between would otherwise
        make the purge treat the moved file as deleted (dropping the migrated
        row + JPEG) and a late batch would re-add the dead old path as a
        ghost tile. Chain a->b then b->c: rewrite any earlier hop that ended
        at this file's old path.
        """
        self._renamed = {
            k: (new_path.as_posix() if v == old_path.as_posix() else v)
            for k, v in self._renamed.items()
        }
        self._renamed[normalize_path(old_path)] = new_path.as_posix()

    # -- scanner results (main thread, queued from worker) -----------------------------

    def _on_scan_items(self, items: list[VideoItem], gen: int) -> None:
        if gen != self._scan_gen or self._closing:
            return  # stale scan, or the window is draining for close
        to_add: list[VideoItem] = []
        to_update: list[VideoItem] = []
        for item in items:
            path_key = normalize_path(item.path)
            if path_key in self._renamed or path_key in self._removed:
                continue  # walked before a rename; the old path is dead
            row = self._model.row_for(item.path)
            if row is None:
                to_add.append(item)
            else:
                current = self._model.item_at(row)
                if current is not None and current.vid != item.vid:
                    # file changed (size/mtime) -> refresh metadata + thumbnail
                    to_update.append(item)
        if to_add:
            # One batched insertion per signal keeps the view cheap even
            # with thousands of items.
            rows = self._model.add_items(to_add)
            for r in rows:
                self._thumbs.request(self._model.item_at(r))
        if to_update:
            # By path, never by the rows resolved above: inserting the batch
            # above may already have moved them.
            self._model.update_items(to_update)
            for item in to_update:
                self._thumbs.request(item)  # a changed file needs a new thumb
        self._update_status()

    def _on_scan_finished(self, result: ScanResult, gen: int) -> None:
        if gen != self._scan_gen or self._closing:
            return
        for old_key, new_p in self._renamed.items():
            # A scan that walked before the rename writes its scan row when
            # it *finishes* — possibly after the rename-time patch in
            # _rename_selected. Repair it now so the next launch does not
            # replay the dead path from the scan cache.
            self._db.rename_scan_entry(Path(old_key).as_posix(), new_p)
        fresh = result.fresh
        if self._renamed:
            # The walk predated the rename(s): swap the old paths for the
            # current files, re-stat'd now so the vid the purge compares
            # matches what cache.rename stored. A file that vanished after
            # the rename simply drops out of the list — purging it is right.
            fixed: dict[str, tuple[int, float]] = {}
            for p, sm in fresh.items():
                new_p = self._renamed.get(normalize_path(Path(p)))
                if new_p is None:
                    fixed[p] = sm
                    continue
                try:
                    st = Path(new_p).stat()
                except OSError:
                    continue
                fixed[Path(new_p).as_posix()] = (st.st_size, st.st_mtime)
            fresh = fixed
        if self._removed:
            # The worker saved its scan row before emitting ``finished`` and
            # may have walked a file just before this window deleted it.
            # Repair both its in-memory truth and the just-written scan row.
            fresh = {
                p: sm
                for p, sm in fresh.items()
                if normalize_path(Path(p)) not in self._removed
            }
            self._db.remove_scan_entries(
                [Path(p).as_posix() for p in self._removed]
            )
        removed = self._cache.purge_folder(result.folder, fresh)
        if removed:
            log.info("purged %d stale cache entries", removed)
        # Converge the grid with the disk: rows the walk did not find —
        # files moved or deleted externally (file-manager drag, another
        # window, the Trash) — must not linger as ghost tiles. The scan
        # cache replay re-adds them on every reopen and nothing else ever
        # removes model rows, so this is the reconciliation point; *fresh*
        # is the walk's truth (renames already remapped above).
        present = {normalize_path(Path(p)) for p in fresh}
        stale = [
            item.path
            for r in range(self._model.count())
            if (item := self._model.item_at(r)) is not None
            and normalize_path(item.path) not in present
        ]
        if stale:
            log.info("dropping %d vanished tile(s) after scan", len(stale))
            self._model.remove_items(stale)
            self._grid.clear_hover()
        self._model.set_layout_deferred(False)  # settle into the sort order
        self._update_status()

    def _on_scan_error(self, message: str) -> None:
        if self._closing:
            return  # a dying scan's error is not something the user can act on
        self._model.set_layout_deferred(False)  # never leave the order pending
        self._status_label.setText(f"Scan failed: {message}")

    def _update_status(self) -> None:
        n = self._model.count()
        self._status_label.setText(f"{n} video{'s' if n != 1 else ''}")

    # -- thumbnail results --------------------------------------------------------------

    def _on_thumb_ready(self, item: VideoItem) -> None:
        if self._closing:
            return  # no model mutation while the window drains for close
        if normalize_path(item.path) in self._removed:
            # The job may have written its DB row after deletion cleaned the
            # cache. Retire that late row/JPEG instead of stranding it.
            self._cache.remove_items([item])
            return
        row = self._model.row_for(item.path)
        if row is None:
            return
        current = self._model.item_at(row)
        if current is None or current.vid != item.vid:
            return  # folder moved on underneath us; ignore stale result
        self._model.update_item(row, item)

    def _on_thumb_failed(self, path: str) -> None:
        log.warning("thumbnail failed for %s", path)

    # -- tile actions (selection-driven) --------------------------------------------------

    def _delete_selected(self, permanent: bool) -> None:
        """Delete the selected files and reconcile every local cache.

        Plain Delete uses Qt's native Trash/Recycle Bin integration.
        Shift+Delete unlinks only after an irreversible-action warning.
        Individual failures are reported together; successful siblings are
        still removed from the grid and caches.
        """
        if self._closing:
            return
        items = [
            item
            for index in self._grid.selectionModel().selectedRows()
            if (item := self._model.item_at(index.row())) is not None
        ]
        if not items:
            return
        if permanent and not delete_dialog.confirm_permanent_delete(
            self, [item.filename for item in items]
        ):
            return

        # Windows cannot rename, trash, or unlink the file while the shared
        # player still owns it. Release before touching any selected path.
        self._player.leave()
        self._grid.clear_hover()

        deleted: list[VideoItem] = []
        failed: list[str] = []
        for item in items:
            try:
                if permanent:
                    try:
                        item.path.unlink()
                    except FileNotFoundError:
                        pass  # already gone: still reconcile its ghost tile
                    success = True
                else:
                    success = move_to_trash(item.path)
            except OSError as exc:
                failed.append(f"{item.filename}: {exc.strerror or exc}")
                continue
            if success:
                deleted.append(item)
            else:
                failed.append(f"{item.filename}: the system refused the operation")

        if deleted:
            paths = [item.path for item in deleted]
            self._removed.update(normalize_path(path) for path in paths)
            self._thumbs.cancel_pending(deleted)
            self._cache.remove_items(deleted)
            self._db.remove_scan_entries([path.as_posix() for path in paths])
            self._model.remove_items(paths)
            self._update_status()
            log.info(
                "%s %d video(s)",
                "permanently deleted" if permanent else "moved to trash",
                len(deleted),
            )
        if failed:
            action = "permanently delete" if permanent else "move to Trash"
            QMessageBox.warning(
                self,
                config.APP_NAME,
                f"Could not {action}:\n" + "\n".join(failed),
            )

    def _rename_selected(self) -> None:
        """F2: rename the one selected video file (stem only, ext fixed).

        Renaming changes the file's cache identity (``vid`` hashes the
        path), so the cached row + JPEG are migrated to the new identity
        (rule 4: no re-extraction), the scan cache is patched (a reopening
        must not replay the dead path), and the model row is replaced —
        its resort moves the tile into the new sort position and the
        persistent-index remap carries the selection along.
        """
        if self._closing:
            return
        rows = sorted(
            i.row() for i in self._grid.selectionModel().selectedRows()
        )
        if len(rows) != 1:
            return  # rename is a single-file action (v1)
        row = rows[0]
        item = self._model.item_at(row)
        if item is None:
            return
        stem = rename_dialog.ask_new_stem(self, item.filename)
        if stem is None:
            return
        _, ext = rename_dialog.split_stem(item.filename)
        new_path = item.path.with_name(stem + ext)
        # Exact string compare: Path equality is case-insensitive on
        # Windows, where a case-only rename IS a real change.
        if new_path.name == item.path.name:
            return
        self._player.leave()
        self._grid.clear_hover()
        try:
            item.path.rename(new_path)
        except FileExistsError:
            # A different file already owns the name. exists() cannot make
            # this call before the rename: on a case-insensitive filesystem
            # it also answers True for a case-only rename of this very file
            # (a.mp4 -> A.mp4), which is legal. The rename itself is the
            # race-free check.
            QMessageBox.warning(
                self,
                config.APP_NAME,
                f"A file named {new_path.name} already exists.",
            )
            return
        except OSError as exc:
            QMessageBox.warning(
                self, config.APP_NAME, f"Rename failed: {exc}"
            )
            return

        st = new_path.stat()
        new_item = VideoItem(new_path, st.st_size, st.st_mtime)
        thumb = self._cache.rename(item, new_item)
        if thumb is not None:
            new_item = new_item.with_thumbnail(thumb)
        self._db.rename_scan_entry(
            item.path.as_posix(), new_path.as_posix()
        )
        # Re-resolve: nothing between ask_new_stem and here should have
        # moved rows, but the model's contract says a row is only valid
        # immediately after row_for().
        row = self._model.row_for(item.path)
        if row is None:
            return  # the folder switched under the dialog; file is renamed
        # Record only after the bail-out: _open_folder cleared the map for
        # the new folder, and an entry for the OLD folder must not leak into
        # the new session's scan remapping.
        self._note_moved(item.path, new_path)
        self._model.rename_item(row, new_item)
        if not new_item.thumb_ready:
            self._thumbs.request(new_item)
        self._update_status()

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
