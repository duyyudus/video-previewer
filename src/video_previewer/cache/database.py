"""SQLite-backed persistence for metadata and scan results."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models.video_item import normalize_path

log = logging.getLogger(__name__)

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
        try:
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                _ensure_schema(self._conn)
                self._conn.commit()
        except sqlite3.Error:
            # A corrupt file must not leave this connection (and its OS file
            # handle, which would block quarantine/rename on Windows) behind.
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            raise

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

    def rename_scan_entry(self, old_path: str, new_path: str) -> int:
        """Rewrite cached scan entries pointing at *old_path* to *new_path*.

        The scans table holds one small row per (folder, recursive) key, so
        a full pass is cheaper than deriving which hashed key a renamed file
        belongs to (it may sit in any subfolder of any cached scan). Without
        this, reopening the folder replays the dead path from the scan cache
        and the grid grows a phantom tile next to the renamed one.

        Entries store the walker's original path casing, while callers may
        hold a ``normalize_path`` form (lowercased on Windows), so the
        comparison normalizes both sides — matching the case rules the rest
        of the cache uses.
        """
        self._guard()
        old_key = normalize_path(Path(old_path))
        updated = 0
        with self._lock:
            rows = self._conn.execute("SELECT key, entries FROM scans").fetchall()
            for r in rows:
                try:
                    entries = json.loads(r["entries"])
                except ValueError:
                    continue
                changed = False
                for e in entries:
                    if e.get("path") == new_path:
                        continue  # already current (patched before save)
                    if normalize_path(Path(e.get("path", ""))) == old_key:
                        e["path"] = new_path
                        changed = True
                if changed:
                    self._conn.execute(
                        "UPDATE scans SET entries = ? WHERE key = ?",
                        (json.dumps(entries), r["key"]),
                    )
                    updated += 1
            self._conn.commit()
        return updated

    def remove_scan_entries(self, paths: list[str]) -> int:
        """Drop cached scan entries pointing at *paths* from every scan.

        Used when a file moves out of its folder (drag-to-move): unlike a
        rename there is no replacement path to write in this folder's
        place, and a leftover entry replays the gone file as a ghost tile
        on every reopen. Matching normalizes both sides, like
        :meth:`rename_scan_entry`. Returns the number of entries removed.
        """
        self._guard()
        doomed = {normalize_path(Path(p)) for p in paths}
        removed = 0
        with self._lock:
            rows = self._conn.execute("SELECT key, entries FROM scans").fetchall()
            for r in rows:
                try:
                    entries = json.loads(r["entries"])
                except ValueError:
                    continue
                kept = [
                    e
                    for e in entries
                    if normalize_path(Path(e.get("path", ""))) not in doomed
                ]
                if len(kept) != len(entries):
                    self._conn.execute(
                        "UPDATE scans SET entries = ? WHERE key = ?",
                        (json.dumps(kept), r["key"]),
                    )
                    removed += len(entries) - len(kept)
            self._conn.commit()
        return removed

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


# SQLite result codes that mean "the bytes of this file are unusable". Only
# those justify renaming the file away: note ``sqlite3.OperationalError`` (a
# lock, a busy DB, an unopenable path) *is* a subclass of
# ``sqlite3.DatabaseError``, so the class alone cannot tell corruption from a
# transient access problem.
_CORRUPT_SQLITE_CODES = frozenset({11, 26})  # SQLITE_CORRUPT, SQLITE_NOTADB
_CORRUPT_SQLITE_NAMES = ("SQLITE_CORRUPT", "SQLITE_NOTADB")


def _is_corrupt(exc: sqlite3.Error) -> bool:
    """True when SQLite says the database *file content* is unreadable."""
    name = getattr(exc, "sqlite_errorname", "") or ""
    code = getattr(exc, "sqlite_errorcode", None)
    if name or code is not None:
        return name.startswith(_CORRUPT_SQLITE_NAMES) or code in _CORRUPT_SQLITE_CODES
    # Ancient binding without error codes: fall back to the message text.
    text = str(exc).lower()
    return "not a database" in text or "malformed" in text


def open_database(path: Path) -> Database:
    """Open the metadata DB, fail soft (rule 8): never crash startup.

    The cache is a pure optimization, so a damaged ``metadata.sqlite`` or an
    unwritable cache dir degrades the session instead of killing the app.
    Recovery is limited to one attempt: quarantine the *corrupt* file
    (``*.bak``) and reopen; anything else falls back to an empty in-memory
    database for this session (logged).
    """
    try:
        return Database(path)
    except sqlite3.Error as exc:
        if not _is_corrupt(exc):
            # "database is locked"/"unable to open": another process owns this
            # file, and there is no single-instance guard. Renaming it away
            # would silently destroy *their* writes (on POSIX the rename even
            # succeeds), so leave the file strictly alone.
            log.warning(
                "metadata DB unavailable (%s); running with an empty "
                "in-memory cache for this session",
                exc,
            )
            return Database(Path(":memory:"))
        log.warning(
            "metadata DB unreadable (%s); moving %s aside and retrying once",
            exc,
            path,
        )
    except OSError as exc:
        # Cannot even prepare the directory (read-only/unmounted volume).
        log.warning(
            "cannot prepare metadata DB at %s (%s); running with an empty "
            "in-memory cache for this session",
            path,
            exc,
        )
        return Database(Path(":memory:"))

    _quarantine(path)
    try:
        return Database(path)
    except (sqlite3.Error, OSError) as exc:
        log.warning(
            "metadata DB still unusable after quarantine (%s); running with an "
            "empty in-memory cache for this session",
            exc,
        )
        return Database(Path(":memory:"))


def _quarantine(path: Path) -> None:
    """Move a corrupt DB file (and its WAL sidecars) aside as ``*.bak``.

    An existing ``*.bak`` is overwritten — the freshest corrupt copy is the
    one worth keeping for inspection.
    """
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            try:
                os.replace(p, Path(str(p) + ".bak"))
            except OSError as exc:
                log.warning("could not move %s aside: %s", p, exc)
