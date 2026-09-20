"""A single scanned video file."""

from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass, field, replace
from pathlib import Path

_SYSTEM = platform.system()


def normalize_path(path: Path) -> str:
    """Stable string form of a path (case-insensitive on Windows)."""
    s = path.as_posix()
    if _SYSTEM == "Windows":
        s = s.lower()
    return s


def video_id(path: Path, size: int, modified: float) -> str:
    """Stable cache identity for a video file.

    Derived from absolute path + modification time + size, per the plan:
    a thumbnail stays valid as long as none of these change.
    """
    key = f"{normalize_path(path)}|{size}|{modified:.3f}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class VideoItem:
    """One video file in the grid.

    ``duration_ms`` / ``width`` / ``height`` / ``vcodec`` / ``thumbnail_path``
    are filled in asynchronously (metadata probe + thumbnail extraction).
    """

    path: Path
    size: int
    modified: float
    duration_ms: int | None = None
    width: int | None = None
    height: int | None = None
    vcodec: str | None = None
    thumbnail_path: Path | None = None
    thumb_ready: bool = False
    # Memoized cache identity (not part of init/equality/repr). The identity
    # inputs (path|size|mtime) never change on a live instance — ``with_*``
    # produce replacements — so sha1 is computed at most once per item
    # instead of on every ``vid`` access (scan-time dedup touches it O(n)
    # times per item).
    _vid: str = field(init=False, repr=False, compare=False, default="")

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def vid(self) -> str:
        if not self._vid:
            self._vid = video_id(self.path, self.size, self.modified)
        return self._vid

    def with_metadata(
        self,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        vcodec: str | None = None,
    ) -> "VideoItem":
        return self._carry(replace(
            self,
            duration_ms=duration_ms,
            width=width,
            height=height,
            vcodec=vcodec,
        ))

    def with_thumbnail(self, path: Path) -> "VideoItem":
        return self._carry(replace(self, thumbnail_path=path, thumb_ready=True))

    def without_cached_data(self) -> "VideoItem":
        """Return the scanned file identity without derived cache fields."""
        return self._carry(replace(
            self,
            duration_ms=None,
            width=None,
            height=None,
            vcodec=None,
            thumbnail_path=None,
            thumb_ready=False,
        ))

    def _carry(self, new: "VideoItem") -> "VideoItem":
        """Copy the memoized identity into *new*.

        Neither metadata nor the thumbnail is part of a file's identity
        (``path|size|mtime`` only), so a replacement derived from ``self``
        keeps the same ``vid`` and must not rehash — these copies run once per
        file on the hydrate and thumb-ready paths, i.e. thousands of times per
        folder.
        """
        new._vid = self._vid
        return new
