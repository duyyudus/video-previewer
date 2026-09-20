"""Bounded, deduplicated queue for asynchronous thumbnail generation."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .. import config
from ..cache.cache import ThumbnailCache
from ..cache.database import Database
from ..media import metadata, thumbnailer
from ..media.thumbnailer import ExtractOutcome
from ..models.video_item import VideoItem

log = logging.getLogger(__name__)


class ThumbSignals(QObject):
    ready = Signal(object)   # updated VideoItem (metadata + thumbnail)
    failed = Signal(str)     # str(path) - keep placeholder
    job_done = Signal(str)   # vid - internal, returns a worker slot


class _ThumbJob(QRunnable):
    def __init__(
        self,
        item: VideoItem,
        db: Database,
        cache: ThumbnailCache,
        signals: ThumbSignals,
    ) -> None:
        super().__init__()
        self._item = item
        self._db = db
        self._cache = cache
        self._signals = signals
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        item = self._item
        vid = item.vid
        try:
            thumb_path = self._cache.thumbnail_path_for(vid)

            # 1) Cache hit: metadata + thumbnail already on disk. Checked
            # before the failure marker — a usable JPEG always beats it.
            row = self._db.get_video(vid)
            if row is not None:
                if row.thumbnail and Path(row.thumbnail) == thumb_path and thumb_path.exists():
                    updated = item.with_metadata(
                        row.duration_ms, row.width, row.height, row.vcodec
                    ).with_thumbnail(thumb_path)
                    self._signals.ready.emit(updated)
                    return
                if row.failed:
                    # Negative cache: this exact file (same vid) was already
                    # refused by ffmpeg. Don't re-probe, never re-burn ffmpeg.
                    self._signals.failed.emit(str(item.path))
                    return

            # 2) Probe + extract.
            probe = metadata.probe_video(item.path)
            outcome = thumbnailer.extract_thumbnail(
                item.path, thumb_path, probe.duration_ms if probe else None
            )
            if outcome is ExtractOutcome.OK:
                try:
                    self._cache.store(
                        item,
                        thumb_path,
                        probe.duration_ms if probe else None,
                        probe.width if probe else None,
                        probe.height if probe else None,
                        probe.vcodec if probe else None,
                    )
                    if not item.path.exists():
                        # The file was deleted/renamed after ffmpeg produced
                        # the JPEG but before this job could publish it. The
                        # window may already have cleaned the old identity;
                        # retire this late write so it cannot strand a cache
                        # row or thumbnail after a folder switch/close.
                        self._cache.remove_items([item])
                        self._signals.failed.emit(str(item.path))
                        return
                except sqlite3.ProgrammingError:
                    # App is shutting down (DB closed). The thumbnail file
                    # itself was written; the DB row is re-created on the next
                    # request.
                    pass
                updated = (
                    item.with_metadata(
                        probe.duration_ms, probe.width, probe.height, probe.vcodec
                    )
                    if probe
                    else item
                ).with_thumbnail(thumb_path)
                self._signals.ready.emit(updated)
                return

            if outcome is ExtractOutcome.FAILED and item.path.exists():
                # Negative cache: ffmpeg itself refused a file that is really
                # there, so the next launch short-circuits instead of running
                # ffmpeg again. Every other outcome (binary missing, file
                # offline, seek timeout, disk full) says nothing about the
                # file and is deliberately left retryable.
                try:
                    self._cache.store_failed(
                        item,
                        probe.duration_ms if probe else None,
                        probe.width if probe else None,
                        probe.height if probe else None,
                        probe.vcodec if probe else None,
                    )
                except sqlite3.ProgrammingError:
                    pass  # DB closed during shutdown
            self._signals.failed.emit(str(item.path))
        except Exception:  # noqa: BLE001 - fail soft (rule 8)
            # A bad file or a closed DB must never crash the app nor leak the
            # concurrency slot. Log and fall back to the static thumbnail.
            log.exception("thumbnail job failed for %s", item.path)
        finally:
            # Always return the slot so the queue keeps pumping, even when a
            # job dies early (otherwise _inflight leaks and shutdown stalls).
            self._signals.job_done.emit(vid)


class ThumbnailQueue(QObject):
    """Keeps at most ``THUMB_CONCURRENCY`` ffmpeg jobs in flight (FIFO)."""

    def __init__(self, db: Database, cache: ThumbnailCache, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db = db
        self._cache = cache
        self._pool = QThreadPool.globalInstance()
        self._signals = ThumbSignals(self)
        # vid -> item, in insertion order. Dict-keyed so request() dedup is
        # O(1): scanning/hydrating a 10k-folder used to linear-scan a deque
        # while recomputing each pending item's sha1 — O(n²) on the GUI thread.
        self._pending: dict[str, VideoItem] = {}
        self._inflight: set[str] = set()
        self._paused = False
        self.signals = self._signals
        # job_done is emitted from worker threads; the connection is queued
        # automatically because the receiver lives on the GUI thread.
        self._signals.job_done.connect(self._on_job_done)

    # -- public API (main thread) --------------------------------------------

    def request(self, item: VideoItem) -> None:
        if item.thumb_ready or self._paused:
            return
        if not self._cache.usable:
            # Unwritable cache dir: every job would probe the file and then
            # fail to write its JPEG. Drop the request instead of burning one
            # ffprobe (plus a logged traceback) per file, every launch.
            return
        vid = item.vid
        if vid in self._inflight or vid in self._pending:
            return
        self._pending[vid] = item
        self._pump()

    def clear_pending(self) -> None:
        """Drop queued-but-not-yet-started jobs (folder switch).

        In-flight jobs are left alone — they must finish to return their
        slot — but work for the abandoned folder never starts.
        """
        self._pending.clear()

    def pause(self) -> None:
        """Stop accepting work and discard jobs that have not started."""
        self._paused = True
        self.clear_pending()

    def resume(self) -> None:
        """Allow thumbnail requests after a coordinated cache operation."""
        self._paused = False
        self._pump()

    def cancel_pending(self, items: list[VideoItem]) -> None:
        """Drop queued work for files removed from the current folder.

        Jobs already running must finish to return their pool slots. Their
        ready signal lets the window remove any cache row written during a
        deletion race.
        """
        for item in items:
            self._pending.pop(item.vid, None)

    def pending_count(self) -> int:
        """Jobs still queued or in flight (used to drain before shutdown)."""
        return len(self._pending) + len(self._inflight)

    # -- internals -------------------------------------------------------------

    def _on_job_done(self, vid: str) -> None:
        self._inflight.discard(vid)
        self._pump()

    def _pump(self) -> None:
        if self._paused:
            return
        while self._pending and len(self._inflight) < config.THUMB_CONCURRENCY:
            # FIFO: dicts keep insertion order; plain dict.popitem() has no
            # ``last`` kwarg, so take the first key explicitly.
            vid = next(iter(self._pending))
            item = self._pending.pop(vid)
            if vid in self._inflight:
                continue
            self._inflight.add(vid)
            self._pool.start(
                _ThumbJob(item, self._db, self._cache, self._signals)
            )
