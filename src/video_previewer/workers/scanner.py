"""Background folder scanner (QRunnable).

Walks the selected directory off the GUI thread, emits results in batches,
and uses a persistent scan cache so re-opening a folder shows the grid
instantly while a fresh walk refreshes it.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import config
from ..cache.cache import ThumbnailCache
from ..cache.database import Database
from ..models.video_item import VideoItem

log = logging.getLogger(__name__)


class _StaleScan(Exception):
    """Internal control flow: the scan was superseded by a newer one.

    Aborts the walk silently — no ``save_scan``, no emission into the void.
    """


@dataclass(slots=True)
class ScanResult:
    folder: Path
    recursive: bool
    fresh: dict[str, tuple[int, float]] = field(default_factory=dict)
    count: int = 0


class ScanSignals(QObject):
    """All signals carry the scan *generation* so stale scans (a new folder
    opened before this one finished) can be ignored by the UI."""

    items = Signal(list, int)      # list[VideoItem], generation
    finished = Signal(object, int)  # ScanResult, generation
    error = Signal(str)


class Scanner(QRunnable):
    def __init__(
        self,
        folder: Path,
        recursive: bool,
        db: Database,
        cache: ThumbnailCache,
        signals: ScanSignals,
        generation: int,
        is_stale: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__()
        self._folder = folder
        self._recursive = recursive
        self._db = db
        self._cache = cache
        self._signals = signals
        self._gen = generation
        # Cancellation: *is_stale* reports (thread-safely) whether a newer
        # scan has taken over. Without it a superseded scan keeps walking +
        # stat()-ing the old tree — minutes of wasted IO on large or
        # network-mounted folders — before emitting into the void.
        self._is_stale = is_stale
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            self._check_current()
            self._emit_cached()
            fresh = self._walk()
            # A stale scan must not write its scan row either: the fresh scan
            # of the same folder overwrites the key, and another folder's
            # entry is pure waste.
            self._check_current()
            self._db.save_scan(
                self._db.scan_key(self._folder, self._recursive),
                [
                    {"path": p.as_posix(), "size": s, "mtime": m}
                    for p, s, m in fresh
                ],
            )
            for i in range(0, len(fresh), config.SCAN_BATCH):
                chunk = [
                    VideoItem(Path(p), s, m) for p, s, m in fresh[i : i + config.SCAN_BATCH]
                ]
                self._signals.items.emit(chunk, self._gen)
            self._signals.finished.emit(
                ScanResult(
                    folder=self._folder,
                    recursive=self._recursive,
                    fresh={p.as_posix(): (s, m) for p, s, m in fresh},
                    count=len(fresh),
                ),
                self._gen,
            )
        except _StaleScan:
            log.info("scan superseded, dropping: %s", self._folder)
        except sqlite3.ProgrammingError as exc:
            # The window closed while this scan was walking, so the DB is
            # already gone: nothing the user can act on, and the UI may be
            # half-torn down by now (the cancellation hook normally catches
            # this before we touch the DB again).
            log.info("scan aborted, database closing (%s): %s", exc, self._folder)
        except Exception as exc:  # noqa: BLE001 - report anything to the UI
            log.exception("scan failed for %s", self._folder)
            self._signals.error.emit(str(exc))

    # -- internals -----------------------------------------------------------

    def _check_current(self) -> None:
        """Raise :class:`_StaleScan` once a newer scan has started."""
        if self._is_stale is not None and self._is_stale():
            raise _StaleScan

    def _emit_cached(self) -> None:
        self._check_current()
        entries = self._db.load_scan(self._db.scan_key(self._folder, self._recursive))
        if not entries:
            return
        items: list[VideoItem] = []
        for e in entries:
            try:
                item = VideoItem(Path(e["path"]), int(e["size"]), float(e["mtime"]))
            except (KeyError, TypeError, ValueError):
                continue
            items.append(self._cache.hydrate(item))
        if items:
            self._check_current()  # don't hand a dead scan's items to the pool
            self._signals.items.emit(items, self._gen)

    def _walk(self) -> list[tuple[Path, int, float]]:
        found: list[tuple[Path, int, float]] = []
        exts = config.SUPPORTED_EXTENSIONS
        if self._recursive:
            for dirpath, dirnames, filenames in os.walk(self._folder):
                self._check_current()  # per-directory: cheap vs. walking one more dir
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    if os.path.splitext(name)[1].lower() not in exts:
                        continue
                    self._check_current()  # cheap attribute compare vs. a stat()
                    self._stat_into(Path(dirpath) / name, found, exts)
        else:
            try:
                with os.scandir(self._folder) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        if os.path.splitext(entry.name)[1].lower() not in exts:
                            continue
                        self._check_current()
                        self._stat_into(self._folder / entry.name, found, exts)
            except OSError as exc:
                raise RuntimeError(f"cannot read folder: {exc}") from exc
        found.sort(key=lambda e: e[0].name.lower())
        return found

    @staticmethod
    def _stat_into(path: Path, out: list[tuple[Path, int, float]], exts) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        if not st.st_size:
            return  # zero-byte files produce no usable thumbnail
        out.append((path, st.st_size, st.st_mtime))
