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


def test_delete_scans_for_folder(db, tmp_path):
    folder = tmp_path / "vids"
    for rec in (False, True):
        db.save_scan(db.scan_key(folder, rec), [{"path": "/v/a.mp4", "size": 1, "mtime": 1.0}])
    other = tmp_path / "other"
    db.save_scan(db.scan_key(other, False), [{"path": "/o/b.mp4", "size": 1, "mtime": 1.0}])

    removed = db.delete_scans_for_folder(folder)
    assert removed == 2
    assert db.load_scan(db.scan_key(folder, False)) is None
    assert db.load_scan(db.scan_key(folder, True)) is None
    # other folders' scan caches are untouched
    assert db.load_scan(db.scan_key(other, False)) is not None


def test_purge_folder_all_removes_everything(db, cache_dir, tmp_path):
    cache = ThumbnailCache(db)
    folder = tmp_path / "vids"
    folder.mkdir()
    items = [VideoItem(folder / f"{name}.mp4", 10, 1.0) for name in ("a", "b")]
    for item in items:
        thumb = cache.thumbnail_path_for(item.vid)
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"jpeg")
        cache.store(item, thumb, 1000, 320, 180, "h264")
    db.save_scan(db.scan_key(folder, False), [{"path": items[0].path.as_posix(), "size": 10, "mtime": 1.0}])

    removed = cache.purge_folder_all(folder)
    assert removed == 2
    for item in items:
        assert db.get_video(item.vid) is None
        assert cache.thumbnail_path_for(item.vid).exists() is False
    assert db.load_scan(db.scan_key(folder, False)) is None
    assert db.load_scan(db.scan_key(folder, True)) is None


def test_delete_videos_chunks_large_lists(db, cache_dir):
    # H2: SQLite's SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds, so an
    # unchunked ``IN (?, ...)`` on a 10k+ folder raises "too many SQL
    # variables". The chunked delete must purge every row and return every
    # stored thumbnail path.
    cache = ThumbnailCache(db)
    vids: list[str] = []
    thumbs: list[str] = []
    for i in range(1200):
        v = f"v{i}"
        vids.append(v)
        t = str(cache.thumbnail_path_for(v))
        thumbs.append(t)
        db.upsert_video(v, f"/v/{v}.mp4", 1, 1.0, t)

    removed = db.delete_videos(vids)

    assert len(removed) == 1200
    assert set(removed) == set(thumbs)
    assert db.get_video("v0") is None
    assert db.get_video("v1199") is None


def test_videos_under_does_not_match_wildcard_folder(db):
    # H3: SQLite ``LIKE`` treats ``_``/``%`` as wildcards (and is
    # case-insensitive for ASCII), so ``purge_folder`` on ``season_1`` used to
    # silently delete a *different* folder's rows (``seasonX1``, ``season-1``).
    # The Python-side filter must only match a real subpath.
    db.upsert_video("a", "/v/season_1/a.mp4", 1, 1.0, "/t/a.jpg")
    db.upsert_video("b", "/v/seasonX1/b.mp4", 1, 1.0, "/t/b.jpg")
    db.upsert_video("c", "/v/season-1/c.mp4", 1, 1.0, "/t/c.jpg")
    db.upsert_video("d", "/v/season_1/sub/d.mp4", 1, 1.0, "/t/d.jpg")

    rows = db.videos_under("/v/season_1")
    paths = {r.path for r in rows}

    # the folder itself and its real subfolder are kept
    assert "/v/season_1/a.mp4" in paths
    assert "/v/season_1/sub/d.mp4" in paths
    # lookalike sibling folders are never matched
    assert "/v/seasonX1/b.mp4" not in paths
    assert "/v/season-1/c.mp4" not in paths


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
