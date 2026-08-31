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
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_videos_path ON videos (path);

CREATE TABLE IF NOT EXISTS scans (
    key        TEXT PRIMARY KEY,
    entries    TEXT NOT NULL,
    scanned_at REAL NOT NULL
);
"""


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
            self._conn.executescript(_SCHEMA)
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
    ) -> None:
        self._guard()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO videos (vid, path, size, mtime, duration_ms, width,
                                    height, vcodec, thumbnail, created)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(vid) DO UPDATE SET
                    path = excluded.path,
                    size = excluded.size,
                    mtime = excluded.mtime,
                    duration_ms = excluded.duration_ms,
                    width = excluded.width,
                    height = excluded.height,
                    vcodec = excluded.vcodec,
                    thumbnail = excluded.thumbnail
                """,
                (vid, path, size, mtime, duration_ms, width, height, vcodec,
                 thumbnail, time.time()),
            )
            self._conn.commit()

    def delete_videos(self, vids: list[str]) -> list[str]:
        """Delete rows, returning the thumbnail paths that were stored."""
        self._guard()
        if not vids:
            return []
        qmark = ",".join("?" for _ in vids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT thumbnail FROM videos WHERE vid IN ({qmark})", vids
            ).fetchall()
            self._conn.execute(f"DELETE FROM videos WHERE vid IN ({qmark})", vids)
            self._conn.commit()
        return [r["thumbnail"] for r in rows]

    def videos_under(self, folder_prefix: str) -> list[VideoRow]:
        """All rows whose stored path is under *folder_prefix* (inclusive)."""
        self._guard()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM videos WHERE path LIKE ?", (folder_prefix + "%",)
            ).fetchall()
        return [self._to_row(r) for r in rows]

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
