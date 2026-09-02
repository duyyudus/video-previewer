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
