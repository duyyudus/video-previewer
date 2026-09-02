"""Database + ThumbnailCache: persistence, stale-entry purge, fail-soft open."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from video_previewer.cache.cache import ThumbnailCache
from video_previewer.cache.database import Database, open_database
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


# -- negative-cache flag + fail-soft startup (audit M3, M6) ----------------------


def test_failed_flag_roundtrip(db):
    # The "known broken file" marker lives in its own column, so "no thumbnail
    # yet" (thumbnail='') stays distinguishable from "ffmpeg refused this file"
    # instead of overloading one sentinel value.
    db.upsert_video("v1", "/v/a.mp4", 100, 1.0, "", 5000, 320, 180, "h264", failed=True)
    row = db.get_video("v1")
    assert row is not None
    assert row.failed is True
    assert row.thumbnail == ""
    assert row.duration_ms == 5000  # metadata probed before the failure survives

    # a later success for the same vid must clear the marker
    db.upsert_video("v1", "/v/a.mp4", 100, 1.0, "/t/v1.jpg", 5000)
    row = db.get_video("v1")
    assert row is not None and row.failed is False


def test_old_database_is_upgraded_in_place(cache_dir):
    # Databases written before the negative cache have no ``failed`` column:
    # opening one upgrades it and keeps every existing row.
    path = cache_dir / "metadata.sqlite"
    legacy = sqlite3.connect(str(path))
    legacy.execute(
        "CREATE TABLE videos (vid TEXT PRIMARY KEY, path TEXT NOT NULL,"
        " size INTEGER NOT NULL, mtime REAL NOT NULL, duration_ms INTEGER,"
        " width INTEGER, height INTEGER, vcodec TEXT, thumbnail TEXT NOT NULL,"
        " created REAL NOT NULL)"
    )
    legacy.execute(
        "INSERT INTO videos VALUES ('v1','/v/a.mp4',1,1.0,2000,320,180,'h264',"
        "'/t/v1.jpg',0.0)"
    )
    legacy.commit()
    legacy.close()

    db = Database(path)
    try:
        row = db.get_video("v1")
        assert row is not None
        assert row.failed is False and row.duration_ms == 2000
        db.upsert_video("v1", "/v/a.mp4", 1, 1.0, "", failed=True)
        assert db.get_video("v1").failed is True
    finally:
        db.close()


def test_open_database_quarantines_corrupt_file(cache_dir):
    # A corrupt metadata.sqlite used to raise straight out of
    # MainWindow.__init__ and kill startup (violating rule 8). open_database
    # must move the corrupt file aside and reopen a fresh one.
    path = cache_dir / "metadata.sqlite"
    path.write_bytes(b"definitely not a sqlite database" * 64)

    db = open_database(path)
    try:
        db.upsert_video("v1", "/v/a.mp4", 100, 1.0, "/t/v1.jpg")
        assert db.get_video("v1") is not None
    finally:
        db.close()

    assert (cache_dir / "metadata.sqlite.bak").read_bytes().startswith(b"definitely")
    # the recovered DB is a normal, persistent one: the next startup reopens it
    db2 = open_database(path)
    try:
        assert db2.get_video("v1") is not None
    finally:
        db2.close()


def test_open_database_does_not_quarantine_a_busy_db(cache_dir, monkeypatch):
    # "database is locked" is an OperationalError, not corruption: another
    # instance owns the file (there is no single-instance guard). Renaming it
    # away would destroy *their* writes — and unlike Windows, POSIX lets the
    # rename succeed — so only an unreadable file may be quarantined.
    from video_previewer.cache import database as dbmod

    path = cache_dir / "metadata.sqlite"
    Database(path).close()  # a real, healthy database owned by another run

    real = dbmod.Database

    def locked(p):
        if str(p) != ":memory:":
            raise sqlite3.OperationalError("database is locked")
        return real(p)

    quarantined: list[Path] = []
    monkeypatch.setattr(dbmod, "Database", locked)
    monkeypatch.setattr(dbmod, "_quarantine", quarantined.append)

    db = dbmod.open_database(path)
    try:
        db.upsert_video("v1", "/v/a.mp4", 100, 1.0, "/t/v1.jpg")
        assert db.get_video("v1") is not None  # degraded: in-memory this session
    finally:
        db.close()

    assert quarantined == []
    assert path.exists()
    assert not (cache_dir / "metadata.sqlite.bak").exists()


def test_open_database_falls_back_to_memory(cache_dir, monkeypatch):
    # If even quarantine+retry can't produce a usable on-disk DB (e.g. an
    # unwritable volume), the app must still start — on an in-memory cache.
    from video_previewer.cache import database as dbmod

    real = dbmod.Database

    def broken(path):
        if str(path) != ":memory:":
            # A corruption-shaped error, so the quarantine path is exercised
            # and even recovery cannot help here.
            raise sqlite3.DatabaseError("file is not a database")
        return real(path)

    monkeypatch.setattr(dbmod, "Database", broken)

    db = dbmod.open_database(cache_dir / "metadata.sqlite")
    try:
        db.upsert_video("v1", "/v/a.mp4", 100, 1.0, "/t/v1.jpg")
        assert db.get_video("v1").path == "/v/a.mp4"
    finally:
        db.close()


def test_unwritable_cache_dir_degrades_but_starts(cache_dir, monkeypatch):
    # Audit M7: ThumbnailCache must not crash startup when its directory
    # cannot be created — and it must report itself unusable so the queue can
    # drop requests instead of running a doomed probe/extract per file.
    from video_previewer.cache import cache as cache_mod

    real_mkdir = Path.mkdir

    def failing_mkdir(self, *args, **kwargs):
        if self.name == "thumbnails":
            raise PermissionError("read-only volume")
        return real_mkdir(self, *args, **kwargs)

    db = Database(cache_dir / "metadata.sqlite")
    monkeypatch.setattr(Path, "mkdir", failing_mkdir)
    try:
        cache = cache_mod.ThumbnailCache(db)
    finally:
        monkeypatch.setattr(Path, "mkdir", real_mkdir)

    assert cache.usable is False
    db.close()
