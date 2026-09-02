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


def test_vid_is_memoized_per_instance(monkeypatch):
    # M1: vid used to recompute sha1 on every access; scan-time dedup on a
    # 10k folder made that quadratic on the GUI thread. The identity inputs
    # (path|size|mtime) never change on a live instance, so it is computed
    # at most once per item.
    from dataclasses import replace

    import video_previewer.models.video_item as vi_mod

    calls: list = []
    real = vi_mod.video_id

    def counting(path, size, modified):
        calls.append((path, size, modified))
        return real(path, size, modified)

    monkeypatch.setattr(vi_mod, "video_id", counting)
    item = vi_mod.VideoItem(Path("/v/a.mp4"), 10, 1.0)
    first = item.vid
    assert item.vid == first and item.vid == first
    assert len(calls) == 1  # memoized after the first access

    # Metadata and thumbnails are not part of a file's identity, so the derived
    # copies on the hydrate / thumb-ready paths carry the memo (that path runs
    # once per file for a whole folder).
    derived = item.with_metadata(1000, 320, 180).with_thumbnail(Path("/t.jpg"))
    assert derived.vid == first
    assert calls == [(item.path, 10, 1.0)]  # not a single rehash

    # A genuinely different identity (replace() bypassing the with_* helpers)
    # still hashes fresh.
    changed = replace(item, size=99)
    assert changed.vid != first
    assert len(calls) == 2


def test_normalize_path_case_on_windows():
    p = Path("C:/Videos/Movie.mp4")
    if platform.system() == "Windows":
        assert normalize_path(p) == "c:/videos/movie.mp4"
    else:
        assert normalize_path(p) == "C:/Videos/Movie.mp4"
