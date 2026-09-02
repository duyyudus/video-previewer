"""SQLite-backed persistence for metadata and scan results."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.video_item import normalize_path

# SQLite binds at most SQLITE_MAX_VARIABLE_NUMBER parameters per statement
# (999 on older builds, 32766 on modern ones). Chunk large vid lists so a
# 10k+ folder purge never exceeds the limit.
_DELETE_CHUNK = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    vid         TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    duration_ms INTEGER,
    width       INTEGER,
    height      INTEGER,
    vcodec      TEXT,
    thumbnail   TEXT NOT NULL,
    failed      INTEGER NOT NULL DEFAULT 0,
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_path ON videos (path);

CREATE TABLE IF NOT EXISTS scans (
    key        TEXT PRIMARY KEY,
    entries    TEXT NOT NULL,
    scanned_at REAL NOT NULL
);
"""


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema, adding columns introduced after the first release.

    ``ALTER TABLE ... ADD COLUMN`` is the whole migration story: this DB is a
    disposable cache, so an old row is either upgraded in place or simply
    re-derived on the next scan.
    """
    conn.executescript(_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    if "failed" not in columns:
        # Databases written before the negative cache: an explicit column (not
        # an empty ``thumbnail`` value) keeps "no thumbnail yet" separate from
        # "ffmpeg refused this file", and leaves room for a future retry count.
        conn.execute(
            "ALTER TABLE videos ADD COLUMN failed INTEGER NOT NULL DEFAULT 0"
        )


@dataclass(slots=True)
class VideoRow:
    vid: str
    path: str
    size: int
    mtime: float
    duration_ms: int | None
    width: int | None
    height: int | None
    vcodec: str | None
    thumbnail: str
    # True when ffmpeg itself refused this exact file (size|mtime): the
    # thumbnail job short-circuits instead of re-burning ffmpeg every launch.
    failed: bool = False


class Database:
    """Thin SQLite wrapper.

    All access is guarded by a lock; the connection is created with
    ``check_same_thread=False`` because scanner/thumbnail workers and the
    GUI thread share it. WAL mode keeps concurrent reads fast.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            _ensure_schema(self._conn)
            self._conn.commit()

    # -- lifecycle ---------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def _guard(self) -> None:
        # Workers may outlive the window; fail soft instead of raising.
        if self._closed:
            raise sqlite3.ProgrammingError("database closed")

    # -- videos -------------------------------------------------------------

    def get_video(self, vid: str) -> VideoRow | None:
        self._guard()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM videos WHERE vid = ?", (vid,)
            ).fetchone()
        return self._to_row(row)

    def upsert_video(
        self,
        vid: str,
        path: str,
        size: int,
        mtime: float,
        thumbnail: str,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        vcodec: str | None = None,
        failed: bool = False,
    ) -> None:
        self._guard()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO videos (vid, path, size, mtime, duration_ms, width,
                                    height, vcodec, thumbnail, failed, created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vid) DO UPDATE SET
                    path = excluded.path,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    duration_ms = excluded.duration_ms,
                    width = excluded.width,
                    height = excluded.height,
                    vcodec = excluded.vcodec,
                    thumbnail = excluded.thumbnail,
                    failed = excluded.failed
                """,
                (vid, path, size, mtime, duration_ms, width, height, vcodec,
                 thumbnail, int(failed), time.time()),
            )
            self._conn.commit()

    def delete_videos(self, vids: list[str]) -> list[str]:
        """Delete rows, returning the thumbnail paths that were stored."""
        self._guard()
        if not vids:
            return []
        thumbs: list[str] = []
        with self._lock:
            for i in range(0, len(vids), _DELETE_CHUNK):
                chunk = vids[i : i + _DELETE_CHUNK]
                qmark = ",".join("?" for _ in chunk)
                rows = self._conn.execute(
                    f"SELECT thumbnail FROM videos WHERE vid IN ({qmark})", chunk
                ).fetchall()
                self._conn.execute(
                    f"DELETE FROM videos WHERE vid IN ({qmark})", chunk
                )
                thumbs.extend(r["thumbnail"] for r in rows)
            self._conn.commit()
        return thumbs

    def videos_under(self, folder_prefix: str) -> list[VideoRow]:
        """All rows whose stored path is under *folder_prefix* (inclusive).

        Filtered in Python (not ``LIKE``): SQLite ``LIKE`` is case-insensitive
        for ASCII and treats ``_``/``%`` as wildcards, which would let a
        ``purge_folder`` silently match and delete a *different* folder's rows
        (e.g. ``C:/videos/season_1`` matching ``C:/videos/seasonX1``). The
        Python-side comparison reuses ``normalize_path`` so case handling
        matches the rest of the cache (case-insensitive on Windows,
        case-sensitive elsewhere) and bounds the match to a real subpath.
        """
        self._guard()
        with self._lock:
            rows = self._conn.execute("SELECT * FROM videos").fetchall()
        folder = normalize_path(Path(folder_prefix)).rstrip("/")
        prefix = folder + "/"
        result: list[VideoRow] = []
        for r in rows:
            p = normalize_path(Path(r["path"]))
            if p == folder or p.startswith(prefix):
                result.append(self._to_row(r))
        return result

    @staticmethod
    def _to_row(row: sqlite3.Row | None) -> VideoRow | None:
        if row is None:
            return None
        return VideoRow(
            vid=row["vid"],
            path=row["path"],
            size=row["size"],
            mtime=row["mtime"],
            duration_ms=row["duration_ms"],
            width=row["width"],
            height=row["height"],
            vcodec=row["vcodec"],
            thumbnail=row["thumbnail"],
            failed=bool(row["failed"]),
        )

    # -- scan cache ----------------------------------------------------------

    @staticmethod
    def scan_key(folder: Path, recursive: bool) -> str:
        key = folder.absolute().as_posix()
        if platform.system() == "Windows":
            key = key.lower()
        h = hashlib.sha1()
        h.update(key.encode("utf-8"))
        h.update(b"|recursive=" + (b"1" if recursive else b"0"))
        return h.hexdigest()

    def load_scan(self, key: str) -> list[dict[str, Any]] | None:
        self._guard()
        with self._lock:
            row = self._conn.execute(
                "SELECT entries FROM scans WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["entries"])
        except ValueError:
            return None

    def delete_scans_for_folder(self, folder: Path) -> int:
        """Delete cached scan results for *folder* (flat + recursive keys)."""
        self._guard()
        keys = (self.scan_key(folder, False), self.scan_key(folder, True))
        with self._lock:
            cur = self._conn.execute("DELETE FROM scans WHERE key IN (?, ?)", keys)
            self._conn.commit()
        return cur.rowcount

    def save_scan(self, key: str, entries: list[dict[str, Any]]) -> None:
        self._guard()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO scans (key, entries, scanned_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    entries = excluded.entries,
                    scanned_at = excluded.scanned_at
                """,
                (key, json.dumps(entries), time.time()),
            )
            self._conn.commit()
