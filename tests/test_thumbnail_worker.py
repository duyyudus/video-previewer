"""Thumbnail worker robustness: fail-soft guards, queue bookkeeping and the
negative cache (audit H1, M2, M3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_previewer import config
from video_previewer.cache.cache import ThumbnailCache
from video_previewer.cache.database import Database
from video_previewer.media import metadata, thumbnailer
from video_previewer.media.thumbnailer import ExtractOutcome
from video_previewer.models.video_item import VideoItem
from video_previewer.workers.thumbnail_worker import (
    ThumbnailQueue,
    ThumbSignals,
    _ThumbJob,
)


@pytest.fixture
def db(cache_dir):
    d = Database(cache_dir / "metadata.sqlite")
    yield d
    d.close()


def _signals() -> tuple[ThumbSignals, list, list, list]:
    """A fresh ThumbSignals plus (ready, failed, done) collectors."""
    signals = ThumbSignals()
    ready: list[VideoItem] = []
    failed: list[str] = []
    done: list[str] = []
    signals.ready.connect(ready.append)
    signals.failed.connect(failed.append)
    signals.job_done.connect(done.append)
    return signals, ready, failed, done


def test_job_done_emitted_when_db_closed(qapp, cache_dir, db):
    """A job whose DB is closed mid-flight still returns its slot exactly once.

    ``get_video`` raises ``sqlite3.ProgrammingError`` on a closed Database;
    without the try/except/finally this would escape the worker and skip
    ``job_done``, leaking the ``_inflight`` slot. The finally must always
    emit ``job_done`` (and never emit it a second time).
    """
    cache = ThumbnailCache(db)
    item = VideoItem(Path("/v/broken.mp4"), 10, 1.0)
    db.upsert_video(
        item.vid,
        item.path.as_posix(),
        item.size,
        item.modified,
        str(cache.thumbnail_path_for(item.vid)),
    )
    db.close()

    signals, ready, failed, done = _signals()
    _ThumbJob(item, db, cache, signals).run()

    assert done == [item.vid]  # exactly once
    assert ready == [] and failed == []


# -- queue bookkeeping (audit M2: dict-keyed pending, clear_pending) ----------


def test_queue_dedups_pending_and_clears_on_demand(qapp, cache_dir, monkeypatch):
    # Concurrency 0 keeps every request pending so the bookkeeping can be
    # observed without running real ffmpeg jobs.
    monkeypatch.setattr(config, "THUMB_CONCURRENCY", 0)
    db = Database(cache_dir / "metadata.sqlite")
    cache = ThumbnailCache(db)
    q = ThumbnailQueue(db, cache)

    a = VideoItem(Path("/v/a.mp4"), 10, 1.0)
    b = VideoItem(Path("/v/b.mp4"), 20, 2.0)
    q.request(a)
    q.request(b)
    q.request(VideoItem(Path("/v/a.mp4"), 10, 1.0))  # same vid -> deduped
    assert q.pending_count() == 2

    q.request(a.with_thumbnail(Path("/t/a.jpg")))  # thumb_ready -> ignored
    assert q.pending_count() == 2

    q.clear_pending()  # folder switch: queued work for the old folder is dropped
    assert q.pending_count() == 0
    q.request(a)  # the queue keeps working afterwards
    assert q.pending_count() == 1
    db.close()


def test_queue_drops_work_when_cache_dir_unusable(qapp, cache_dir, monkeypatch):
    # Audit M7 follow-up: an unwritable thumbnails dir must not degrade into
    # one doomed job — and one full ffprobe plus a logged traceback — per file
    # on every launch.
    real_mkdir = Path.mkdir

    def failing_mkdir(self, *args, **kwargs):
        if self.name == "thumbnails":
            raise PermissionError("read-only volume")
        return real_mkdir(self, *args, **kwargs)

    db = Database(cache_dir / "metadata.sqlite")
    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    try:
        cache = ThumbnailCache(db)
    finally:
        monkeypatch.setattr(Path, "mkdir", real_mkdir)
    assert cache.usable is False

    probed: list[Path] = []
    monkeypatch.setattr(metadata, "probe_video", lambda p: probed.append(p) or None)
    q = ThumbnailQueue(db, cache)

    q.request(VideoItem(Path("/v/a.mp4"), 10, 1.0))
    q.request(VideoItem(Path("/v/b.mp4"), 20, 2.0))

    assert q.pending_count() == 0
    assert probed == []  # not a single probe was burned
    db.close()


# -- negative cache for refused files (audit M3) ------------------------------


def test_negative_cache_short_circuits_broken_file(qapp, cache_dir, db, monkeypatch):
    """A row marked failed must not re-probe/re-extract — just fail fast."""
    cache = ThumbnailCache(db)
    item = VideoItem(Path("/v/broken.mp4"), 10, 1.0)
    db.upsert_video(
        item.vid, item.path.as_posix(), item.size, item.modified, "", failed=True
    )

    def boom(*args, **kwargs):  # any ffmpeg work from here is a bug
        raise AssertionError("must not re-probe/re-extract a known-bad file")

    monkeypatch.setattr(metadata, "probe_video", boom)
    monkeypatch.setattr(thumbnailer, "extract_thumbnail", boom)

    signals, ready, failed, done = _signals()
    _ThumbJob(item, db, cache, signals).run()

    assert failed == [str(item.path)]
    assert ready == []
    assert done == [item.vid]  # slot returned exactly once


def test_existing_thumbnail_wins_over_failure_marker(qapp, cache_dir, db, monkeypatch):
    # Defensive ordering: a usable JPEG on disk for this vid beats a marker.
    cache = ThumbnailCache(db)
    item = VideoItem(Path("/v/ok.mp4"), 10, 1.0)
    thumb = cache.thumbnail_path_for(item.vid)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpeg")
    db.upsert_video(
        item.vid, item.path.as_posix(), item.size, item.modified,
        str(thumb), 4000, failed=True,
    )

    def boom(*args, **kwargs):
        raise AssertionError("a cache hit must not touch ffmpeg")

    monkeypatch.setattr(metadata, "probe_video", boom)
    monkeypatch.setattr(thumbnailer, "extract_thumbnail", boom)

    signals, ready, failed, done = _signals()
    _ThumbJob(item, db, cache, signals).run()

    assert failed == []
    assert ready and ready[0].thumb_ready and ready[0].thumbnail_path == thumb


def test_decode_failure_is_recorded_for_next_launch(qapp, cache_dir, db, monkeypatch,
                                                    tmp_path):
    """ffmpeg refusing a file that is really there writes the marker row."""
    monkeypatch.setattr(metadata, "probe_video", lambda path: None)
    monkeypatch.setattr(
        thumbnailer, "extract_thumbnail", lambda *a, **k: ExtractOutcome.FAILED
    )

    cache = ThumbnailCache(db)
    p = tmp_path / "bad.mp4"
    p.write_bytes(b"x" * 10)
    item = VideoItem(p, 10, 1.0)
    signals, ready, failed, done = _signals()

    _ThumbJob(item, db, cache, signals).run()

    assert failed == [str(item.path)]
    assert ready == []
    assert done == [item.vid]
    row = db.get_video(item.vid)
    assert row is not None and row.failed is True and row.thumbnail == ""
    # hydrate must not resurrect a phantom thumbnail for it
    fresh = cache.hydrate(VideoItem(p, 10, 1.0))
    assert fresh.thumb_ready is False
    assert fresh.thumbnail_path is None


def test_transient_failure_is_not_recorded(qapp, cache_dir, db, monkeypatch, tmp_path):
    # A transient outcome (missing binary, seek timeout, disk full) says
    # nothing about the file, so it must stay retryable next launch instead of
    # poisoning a vid whose identity never changes.
    monkeypatch.setattr(metadata, "probe_video", lambda path: None)
    monkeypatch.setattr(
        thumbnailer, "extract_thumbnail", lambda *a, **k: ExtractOutcome.TRANSIENT
    )

    cache = ThumbnailCache(db)
    p = tmp_path / "slow-on-network-share.mp4"
    p.write_bytes(b"x" * 10)
    item = VideoItem(p, 10, 1.0)
    signals, ready, failed, done = _signals()

    _ThumbJob(item, db, cache, signals).run()

    assert failed == [str(item.path)]  # still reported this session
    assert ready == []
    assert db.get_video(item.vid) is None  # nothing poisoned for next launch


def test_failure_of_a_vanished_file_is_not_recorded(qapp, cache_dir, db, monkeypatch):
    # The file went away mid-job (deleted, or an offline share): marking its
    # vid permanently broken would survive the drive coming back unchanged.
    monkeypatch.setattr(metadata, "probe_video", lambda path: None)
    monkeypatch.setattr(
        thumbnailer, "extract_thumbnail", lambda *a, **k: ExtractOutcome.FAILED
    )

    cache = ThumbnailCache(db)
    item = VideoItem(Path("/nowhere/offline-share/bad.mp4"), 10, 1.0)
    signals, ready, failed, done = _signals()

    _ThumbJob(item, db, cache, signals).run()

    assert failed == [str(item.path)]
    assert db.get_video(item.vid) is None


def test_failure_not_recorded_when_ffmpeg_missing(qapp, cache_dir, db, monkeypatch):
    """Missing ffmpeg is transient, not a bad file — no negative-cache row.

    Drives the real ``extract_thumbnail`` so the classification itself is
    covered, not just a stubbed outcome.
    """
    monkeypatch.setattr(config, "ffmpeg_path", lambda: None)
    monkeypatch.setattr(metadata, "probe_video", lambda path: None)

    cache = ThumbnailCache(db)
    item = VideoItem(Path("/v/ok-later.mp4"), 10, 1.0)
    signals, ready, failed, done = _signals()

    _ThumbJob(item, db, cache, signals).run()

    assert failed == [str(item.path)]  # still reported this session
    assert ready == []
    assert db.get_video(item.vid) is None  # but nothing poisoned for next launch
