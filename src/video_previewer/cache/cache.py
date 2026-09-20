"""Thumbnail cache policy: path layout, validity checks, cache cleanup."""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config
from ..models.video_item import VideoItem, normalize_path
from .database import Database

log = logging.getLogger(__name__)


def _remove_thumb_file(value: str) -> bool:
    """Unlink a stored thumbnail path; True when a file was actually removed.

    An empty value is *not* a path: metadata-only and failure-marked rows
    store ``thumbnail=''``, and ``Path('')`` is the current directory — so an
    empty value must never reach ``unlink()``.
    """
    if not value:
        return False
    try:
        p = Path(value)
        if p.exists():
            p.unlink()
            return True
    except OSError:
        log.debug("could not remove thumbnail %s", value)
    return False


class ThumbnailCache:
    """Coordinates on-disk thumbnails with the metadata database."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._dir: Path = config.thumbnail_dir()
        self._usable = True
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Fail soft (rule 8): an unwritable cache dir degrades to "no
            # thumbnails" instead of killing startup. ``usable`` lets the
            # queue drop requests entirely rather than run a doomed ffprobe +
            # ffmpeg attempt (and log a traceback) for every single file.
            self._usable = False
            log.warning("cannot create thumbnail cache dir %s (%s)", self._dir, exc)

    @property
    def directory(self) -> Path:
        return self._dir

    @property
    def usable(self) -> bool:
        """False when the thumbnail directory cannot be written at all."""
        return self._usable

    def thumbnail_path_for(self, vid: str) -> Path:
        return self._dir / f"{vid}.jpg"

    def hydrate(self, item: VideoItem) -> VideoItem:
        """Attach cached metadata/thumbnail to *item* when available."""
        row = self._db.get_video(item.vid)
        if row is None:
            return item
        item = item.with_metadata(
            row.duration_ms, row.width, row.height, row.vcodec
        )
        if row.failed or not row.thumbnail:
            return item  # known-broken file, or a row without a thumbnail yet
        thumb = Path(row.thumbnail)
        if thumb.exists():
            item = item.with_thumbnail(thumb)
        return item

    def store_failed(
        self,
        item: VideoItem,
        duration_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        vcodec: str | None = None,
    ) -> None:
        """Record that ffmpeg refused *item* (negative cache).

        Any metadata obtained before the failure is still kept; ``_ThumbJob``
        short-circuits on the ``failed`` flag so the file is never probed or
        re-extracted until its identity (size/mtime/path) changes. Only a
        genuine decode failure may be recorded here — see
        :class:`~video_previewer.media.thumbnailer.ExtractOutcome`.
        """
        self._db.upsert_video(
            vid=item.vid,
            path=item.path.as_posix(),
            size=item.size,
            mtime=item.modified,
            thumbnail="",
            duration_ms=duration_ms,
            width=width,
            height=height,
            vcodec=vcodec,
            failed=True,
        )

    def store(
        self,
        item: VideoItem,
        thumbnail: Path,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        vcodec: str | None,
    ) -> None:
        self._db.upsert_video(
            vid=item.vid,
            path=item.path.as_posix(),
            size=item.size,
            mtime=item.modified,
            thumbnail=str(thumbnail),
            duration_ms=duration_ms,
            width=width,
            height=height,
            vcodec=vcodec,
            failed=False,  # a success always clears an earlier failure
        )

    def rename(self, old: VideoItem, new: VideoItem) -> Path | None:
        """Move a renamed file's cached row + JPEG to its new identity.

        The file itself has already been renamed by the caller; ``vid``
        includes the path, so the old row/thumbnail would otherwise be
        stranded and the renamed file re-probed and re-extracted on the
        next scan (rule 4). Returns the relocated thumbnail path, or None
        when nothing usable was cached.
        """
        row = self._db.get_video(old.vid)
        if row is None:
            return None
        new_thumb: Path | None = None
        if row.thumbnail and not row.failed:
            src_p = Path(row.thumbnail)
            dst_p = self.thumbnail_path_for(new.vid)
            try:
                if src_p.exists():
                    if src_p != dst_p:
                        src_p.replace(dst_p)
                    new_thumb = dst_p
            except OSError:
                log.warning("could not move thumbnail %s -> %s", src_p, dst_p)
        self._db.upsert_video(
            vid=new.vid,
            path=new.path.as_posix(),
            size=new.size,
            mtime=new.modified,
            thumbnail=str(new_thumb) if new_thumb else "",
            duration_ms=row.duration_ms,
            width=row.width,
            height=row.height,
            vcodec=row.vcodec,
            failed=row.failed,
        )
        if new.vid != old.vid:
            # A case-only rename on Windows keeps the vid (paths hash
            # lowercased): the upsert above already updated the row in
            # place, and deleting it would drop what we just wrote.
            self._db.delete_videos([old.vid])
        return new_thumb

    def remove_items(self, items: list[VideoItem]) -> int:
        """Drop exact video rows and their cached thumbnail files."""
        if not items:
            return 0
        for thumbnail in self._db.delete_videos([item.vid for item in items]):
            _remove_thumb_file(thumbnail)
        return len(items)

    def clear_all(self) -> int:
        """Clear all generated thumbnails, metadata, and cached scans.

        Only files directly inside the managed thumbnail directory are
        removed.  This deliberately avoids following directories or stored
        paths from the database when performing a global cleanup.
        """
        self._db.clear_cache()
        removed = 0
        try:
            entries = list(self._dir.iterdir())
        except OSError as exc:
            log.warning("could not list thumbnail cache %s (%s)", self._dir, exc)
            return 0
        for entry in entries:
            try:
                if entry.is_file() or entry.is_symlink():
                    entry.unlink()
                    removed += 1
            except OSError:
                log.warning("could not remove cached thumbnail %s", entry)
        return removed

    def purge_folder(
        self,
        folder: Path,
        fresh: dict[str, tuple[int, float]],
        *,
        recursive: bool,
    ) -> int:
        """Drop in-scope cache entries for files that are gone or changed.

        *fresh* maps ``str(path) -> (size, mtime)`` for every video seen in
        the latest scan of *folder*. A flat scan owns only direct children;
        it must not delete reusable thumbnails belonging to subfolders that
        were outside that scan. Returns the number of rows removed.
        """
        from ..models.video_item import video_id

        rows = self._db.videos_under(folder.absolute().as_posix())
        if not rows:
            return 0
        folder_key = normalize_path(folder.absolute())
        fresh_n = {normalize_path(Path(p)): (s, m) for p, (s, m) in fresh.items()}
        stale: list[str] = []
        for row in rows:
            if not recursive and normalize_path(Path(row.path).parent) != folder_key:
                continue
            np_ = normalize_path(Path(row.path))
            if np_ not in fresh_n:
                # file deleted (or no longer matched)
                stale.append(row.vid)
                continue
            size, mtime = fresh_n[np_]
            if video_id(Path(row.path), size, mtime) != row.vid:
                # file changed since it was cached
                stale.append(row.vid)
        if not stale:
            return 0
        thumbs = self._db.delete_videos(stale)
        for t in thumbs:
            _remove_thumb_file(t)
        return len(stale)

    def purge_folder_all(self, folder: Path) -> int:
        """Drop every cached video and scan entry for *folder*.

        Used when the user discards the folder on exit. Returns the number
        of video rows removed (their thumbnail files are unlinked too).
        """
        rows = self._db.videos_under(folder.absolute().as_posix())
        vids = [r.vid for r in rows]
        if vids:
            for t in self._db.delete_videos(vids):
                _remove_thumb_file(t)
        self._db.delete_scans_for_folder(folder)
        return len(vids)
