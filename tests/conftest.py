"""Shared fixtures: offscreen Qt app, isolated cache dir, ffmpeg helpers."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# Must be set before QApplication is constructed anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from video_previewer import config  # noqa: E402

HAS_FFMPEG = bool(config.ffmpeg_path() and config.ffprobe_path())


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    # Test-specific identity so QSettings never touches the user's app data.
    app.setApplicationName("video-previewer-test")
    app.setOrganizationName("video-previewer-test")
    yield app


@pytest.fixture(autouse=True)
def _clean_settings(qapp):
    """No last-folder restore between tests (isolated scans)."""
    from PySide6.QtCore import QSettings

    s = QSettings()
    s.remove(config.SETTING_LAST_FOLDER)
    s.remove(config.SETTING_RECURSIVE)
    yield
    s.sync()


@pytest.fixture
def cache_dir(qapp, tmp_path, monkeypatch):
    d = tmp_path / "cache"
    d.mkdir()
    monkeypatch.setenv("VIDEO_PREVIEWER_CACHE_DIR", str(d))
    return d


def make_video(
    path: Path,
    seconds: float = 4.0,
    width: int = 320,
    height: int = 180,
    vcodec: str = "libx264",
    acodec: str = "aac",
) -> Path:
    """Generate a tiny real video file with ffmpeg (skipped if unavailable)."""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not available")
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.ffmpeg_path(),
        "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={width}x{height}:rate=24",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", vcodec, "-preset", "ultrafast",
        "-c:a", acodec, "-shortest",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
    return path


def pump(app, condition, timeout: float = 30.0) -> bool:
    """Spin the event loop until *condition()* is true or *timeout* expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        app.processEvents()
        if condition():
            return True
        time.sleep(0.01)
    return False
