"""ffprobe/ffmpeg pipeline: metadata probe + thumbnail extraction."""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from video_previewer import config
from video_previewer.media import metadata, thumbnailer
from video_previewer.media.thumbnailer import ExtractOutcome
from conftest import HAS_FFMPEG, make_video

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not available")


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    p = make_video(tmp_path_factory.mktemp("vids") / "movie.mp4", seconds=5, width=320, height=180)
    return p


def test_probe_metadata(video):
    result = metadata.probe_video(video)
    assert result is not None
    assert 4500 <= result.duration_ms <= 5500  # ~5s
    assert result.width == 320
    assert result.height == 180
    assert result.vcodec == "h264"


def test_probe_missing_file(tmp_path):
    assert metadata.probe_video(tmp_path / "nope.mp4") is None


def test_thumbnail_time():
    assert thumbnailer.thumbnail_time(100_000) == pytest.approx(15.0)
    # short video: midpoint fallback
    assert thumbnailer.thumbnail_time(1500) == pytest.approx(0.75)
    # unknown duration: fixed fallback
    assert thumbnailer.thumbnail_time(None) == config.THUMB_FALLBACK_SECONDS
    # clamped to the last 100 ms of the file
    assert thumbnailer.thumbnail_time(1_000) < 1.0


def test_extract_thumbnail(video, tmp_path):
    out = tmp_path / "thumb.jpg"
    assert thumbnailer.extract_thumbnail(video, out, 5000) is ExtractOutcome.OK
    assert out.exists()
    img = QImage()
    assert img.load(str(out))
    assert img.width() == config.THUMB_WIDTH
    assert img.height() == 180  # 320x180 source keeps aspect


def test_extract_thumbnail_missing_video(tmp_path):
    # An absent file says nothing about the file itself (deleted mid-scan, or a
    # network share that is offline right now), so it must stay retryable.
    out = tmp_path / "thumb.jpg"
    assert thumbnailer.extract_thumbnail(tmp_path / "nope.mp4", out, 5000) is (
        ExtractOutcome.TRANSIENT
    )
    assert not out.exists()
    assert not (tmp_path / "thumb.jpg.tmp").exists()


def test_extract_thumbnail_broken_file(tmp_path):
    # ffmpeg ran to completion and refused the file: the one outcome the
    # negative cache is allowed to record as permanent.
    p = tmp_path / "broken.mp4"
    p.write_bytes(b"not a video at all " * 64)
    out = tmp_path / "thumb.jpg"
    assert thumbnailer.extract_thumbnail(p, out, None) is ExtractOutcome.FAILED
    assert not out.exists()
    assert not (tmp_path / "thumb.jpg.tmp").exists()


def test_extract_thumbnail_unwritable_cache_dir_is_transient(video, tmp_path):
    # A plain file standing where the thumbnail directory should be: the video
    # is fine, so this must never be blamed on it.
    blocker = tmp_path / "thumbnails"
    blocker.write_text("not a directory")
    out = blocker / "thumb.jpg"
    assert thumbnailer.extract_thumbnail(video, out, 5000) is ExtractOutcome.TRANSIENT


def test_extract_thumbnail_webm(tmp_path):
    p = make_video(tmp_path / "m.webm", seconds=4, vcodec="libvpx", acodec="libopus")
    out = tmp_path / "t.webm.jpg"
    assert thumbnailer.extract_thumbnail(p, out) is ExtractOutcome.OK
    assert QImage(out).isNull() is False
