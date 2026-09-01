"""Thumbnail worker robustness: fail-soft exception guard (rule 8).

Covers the H1 high-priority finding: ``_ThumbJob.run()`` had no exception
guard, so a ``sqlite3.ProgrammingError`` from a closed ``Database`` skipped
``job_done`` entirely — leaking the concurrency slot for the life of the
process and stalling the ``closeEvent`` drain loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_previewer.cache.cache import ThumbnailCache
from video_previewer.cache.database import Database
from video_previewer.models.video_item import VideoItem
from video_previewer.workers.thumbnail_worker import ThumbSignals, _ThumbJob


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
