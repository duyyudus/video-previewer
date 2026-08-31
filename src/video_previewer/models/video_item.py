"""A single scanned video file."""

from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass, replace
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

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def vid(self) -> str:
        return video_id(self.path, self.size, self.modified)

    def with_metadata(
        self,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        vcodec: str | None = None,
    ) -> "VideoItem":
        return replace(
            self,
            duration_ms=duration_ms,
            width=width,
            height=height,
            vcodec=vcodec,
        )

    def with_thumbnail(self, path: Path) -> "VideoItem":
        return replace(self, thumbnail_path=path, thumb_ready=True)
