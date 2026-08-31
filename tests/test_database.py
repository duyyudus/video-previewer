"""Database + ThumbnailCache: persistence and stale-entry purge."""

from __future__ import annotations

from pathlib import Path

import pytest

from video_previewer.cache.cache import ThumbnailCache
from video_previewer.cache.database import Database
from video_previewer.models.video_item import VideoItem


@pytest.fixture
def db(cache_dir):
    d = Database(cache_dir / "metadata.sqlite")
    yield d
    d.close()


def test_video_row_roundtrip(db):
    db.upsert_video("vid1", "/v/a.mp4", 100, 1000.0, "/t/vid1.jpg", 5000, 320, 180, "h264")
    row = db.get_video("vid1")
    assert row is not None
    assert row.path == "/v/a.mp4"
    assert row.duration_ms == 5000
    assert row.width == 320
    # upsert overwrites
    db.upsert_video("vid1", "/v/a.mp4", 100, 1000.0, "/t/vid1.jpg", 6000, 640, 360, "h264")
    assert db.get_video("vid1").duration_ms == 6000
    assert db.get_video("missing") is None


def test_scan_cache_roundtrip(db, tmp_path):
    folder = tmp_path / "vids"
    key = db.scan_key(folder, recursive=True)
    assert db.load_scan(key) is None
    entries = [{"path": "/v/a.mp4", "size": 1, "mtime": 1.0}]
    db.save_scan(key, entries)
    assert db.load_scan(key) == entries
    # different recursion flag -> different key
    assert db.load_scan(db.scan_key(folder, recursive=False)) is None


def test_purge_removes_deleted_and_changed(db, cache_dir, tmp_path):
    cache = ThumbnailCache(db)
    folder = tmp_path / "vids"
    folder.mkdir()
    items = {
        "kept": VideoItem(folder / "kept.mp4", 10, 1.0),
        "changed": VideoItem(folder / "changed.mp4", 10, 1.0),
        "deleted": VideoItem(folder / "deleted.mp4", 10, 1.0),
    }
    for item in items.values():
        thumb = cache.thumbnail_path_for(item.vid)
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"jpeg")
        cache.store(item, thumb, 1000, 320, 180, "h264")

    fresh = {
        items["kept"].path.as_posix(): (10, 1.0),
        items["changed"].path.as_posix(): (10, 999.0),  # mtime changed -> stale
    }
    removed = cache.purge_folder(folder, fresh)
    assert removed == 2  # changed + deleted

    assert db.get_video(items["kept"].vid) is not None
    assert db.get_video(items["changed"].vid) is None
    assert db.get_video(items["deleted"].vid) is None
    # orphan thumbnail files are removed too
    assert cache.thumbnail_path_for(items["changed"].vid).exists() is False
    assert cache.thumbnail_path_for(items["deleted"].vid).exists() is False
    assert cache.thumbnail_path_for(items["kept"].vid).exists() is True


def test_hydrate_uses_cache(db, cache_dir):
    cache = ThumbnailCache(db)
    item = VideoItem(Path("/v/h.mp4"), 10, 1.0)
    thumb = cache.thumbnail_path_for(item.vid)
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpeg")
    cache.store(item, thumb, 4321, 1280, 720, "h264")

    fresh = VideoItem(Path("/v/h.mp4"), 10, 1.0)
    hydrated = cache.hydrate(fresh)
    assert hydrated.duration_ms == 4321
    assert hydrated.width == 1280
    assert hydrated.thumb_ready is True
    assert hydrated.thumbnail_path == thumb

    # unknown file -> unchanged
    other = VideoItem(Path("/v/new.mp4"), 5, 2.0)
    assert cache.hydrate(other) is other
