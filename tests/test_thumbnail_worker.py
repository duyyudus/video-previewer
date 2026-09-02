"""Thumbnail worker robustness: fail-soft exception guard (rule 8).

Covers the H1 high-priority finding: ``_ThumbJob.run()`` had no exception
guard, so a ``sqlite3.ProgrammingError`` from a closed ``Database`` skipped
``job_done`` entirely — leaking the concurrency slot for the life of the
process and stalling the ``closeEvent`` drain loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_previewer import config
from video_previewer.cache.cache import ThumbnailCache
from video_previewer.cache.database import Database
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

    signals = ThumbSignals()
    done: list[str] = []
    signals.job_done.connect(done.append)

    _ThumbJob(item, db, cache, signals).run()

    assert done == [item.vid]


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
