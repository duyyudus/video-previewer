"""VideoItem identity and normalization."""

from __future__ import annotations

import platform
from pathlib import Path

from video_previewer.models.video_item import VideoItem, normalize_path, video_id


def test_video_id_is_stable():
    p = Path("/videos/movie.mp4")
    assert video_id(p, 1024, 1700000000.123) == video_id(p, 1024, 1700000000.123)


def test_video_id_changes_with_mtime_or_size():
    p = Path("/videos/movie.mp4")
    base = video_id(p, 1024, 1700000000.0)
    assert base != video_id(p, 1024, 1700000001.0)
    assert base != video_id(p, 2048, 1700000000.0)
    assert base != video_id(Path("/other/movie.mp4"), 1024, 1700000000.0)


def test_video_item_roundtrip():
    item = VideoItem(Path("/v/a.mp4"), 10, 1.0)
    item2 = item.with_metadata(5000, 320, 180, "h264").with_thumbnail(Path("/t/a.jpg"))
    assert item2.duration_ms == 5000
    assert item2.width == 320
    assert item2.vcodec == "h264"
    assert item2.thumb_ready is True
    assert item2.thumbnail_path == Path("/t/a.jpg")
    # original is immutable
    assert item.thumb_ready is False
    assert item.vid == item2.vid


def test_normalize_path_case_on_windows():
    p = Path("C:/Videos/Movie.mp4")
    if platform.system() == "Windows":
        assert normalize_path(p) == "c:/videos/movie.mp4"
    else:
        assert normalize_path(p) == "C:/Videos/Movie.mp4"
