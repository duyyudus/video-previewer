"""Thumbnail cache policy: path layout, validity checks, cache cleanup."""

from __future__ import annotations

import logging
from pathlib import Path

from .. import config
from ..models.video_item import VideoItem, normalize_path
from .database import Database

log = logging.getLogger(__name__)


class ThumbnailCache:
    """Coordinates on-disk thumbnails with the metadata database."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._dir: Path = config.thumbnail_dir()
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._dir

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
        thumb = Path(row.thumbnail)
        if thumb.exists():
            item = item.with_thumbnail(thumb)
        return item

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
        )

    def purge_folder(self, folder: Path, fresh: dict[str, tuple[int, float]]) -> int:
        """Drop cache entries for files that are gone or changed.

        *fresh* maps ``str(path) -> (size, mtime)`` for every video seen in
        the latest scan of *folder*. Returns the number of rows removed.
        """
        from ..models.video_item import video_id

        rows = self._db.videos_under(folder.absolute().as_posix())
        if not rows:
            return 0
        fresh_n = {normalize_path(Path(p)): (s, m) for p, (s, m) in fresh.items()}
        stale: list[str] = []
        for row in rows:
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
        removed = 0
        for t in thumbs:
            try:
                p = Path(t)
                if p.exists():
                    p.unlink()
                    removed += 1
            except OSError:
                log.debug("could not remove stale thumbnail %s", t)
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
                try:
                    p = Path(t)
                    if p.exists():
                        p.unlink()
                except OSError:
                    log.debug("could not remove thumbnail %s", t)
        self._db.delete_scans_for_folder(folder)
        return len(vids)
