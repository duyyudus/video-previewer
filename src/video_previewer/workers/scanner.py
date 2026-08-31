"""Background folder scanner (QRunnable).

Walks the selected directory off the GUI thread, emits results in batches,
and uses a persistent scan cache so re-opening a folder shows the grid
instantly while a fresh walk refreshes it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import config
from ..cache.cache import ThumbnailCache
from ..cache.database import Database
from ..models.video_item import VideoItem

log = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__()
        self._folder = folder
        self._recursive = recursive
        self._db = db
        self._cache = cache
        self._signals = signals
        self._gen = generation
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        try:
            self._emit_cached()
            fresh = self._walk()
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
        except Exception as exc:  # noqa: BLE001 - report anything to the UI
            log.exception("scan failed for %s", self._folder)
            self._signals.error.emit(str(exc))

    # -- internals -----------------------------------------------------------

    def _emit_cached(self) -> None:
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
            self._signals.items.emit(items, self._gen)

    def _walk(self) -> list[tuple[Path, int, float]]:
        found: list[tuple[Path, int, float]] = []
        exts = config.SUPPORTED_EXTENSIONS
        if self._recursive:
            for dirpath, dirnames, filenames in os.walk(self._folder):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                for name in filenames:
                    if name.startswith("."):
                        continue
                    if os.path.splitext(name)[1].lower() not in exts:
                        continue
                    self._stat_into(Path(dirpath) / name, found, exts)
        else:
            try:
                with os.scandir(self._folder) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        if os.path.splitext(entry.name)[1].lower() not in exts:
                            continue
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
