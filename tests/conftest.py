"""Shared fixtures: offscreen Qt app, isolated cache dir, ffmpeg helpers."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

# Must be set before QApplication is constructed anywhere.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

if os.name == "nt":
    # The DSH file sandbox (active when tests run inside a DSH session)
    # makes directories created with an explicit ``mode`` argument
    # inaccessible afterwards (scandir/rmdir are denied), which breaks
    # pytest's tmpdir plugin: it creates its basetemp via
    # ``Path.mkdir(mode=0o700)``. ``mode`` is ignored by plain Windows
    # ``mkdir`` anyway, so drop it here so pytest's temp dirs stay usable.
    _real_mkdir = os.mkdir

    def _mkdir_ignore_mode(name, mode=0o777):
        return _real_mkdir(name)

    os.mkdir = _mkdir_ignore_mode

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
    """No persisted settings between tests (isolated scans, default window)."""
    from PySide6.QtCore import QSettings

    s = QSettings()
    s.remove(config.SETTING_LAST_FOLDER)
    s.remove(config.SETTING_RECURSIVE)
    s.remove(config.SETTING_WINDOW_GEOMETRY)
    s.remove(config.SETTING_SIDEBAR_VISIBLE)
    s.remove(config.SETTING_SIDEBAR_SPLITTER)
    yield
    s.sync()


@pytest.fixture
def file_settings(tmp_path, monkeypatch):
    """Point MainWindow's QSettings at a file (INI format).

    The DSH file sandbox silently blocks QSettings persistence in the native
    (registry) format, so the close-behavior tests run against a file-backed
    settings store instead (works everywhere, readable from fresh instances).
    """
    from PySide6.QtCore import QSettings
    from video_previewer.ui import main_window as main_window_mod

    path = str(tmp_path / "settings.ini")

    class FileSettings(QSettings):
        def __init__(self) -> None:
            super().__init__(path, QSettings.Format.IniFormat)

    monkeypatch.setattr(main_window_mod, "QSettings", FileSettings)
    return path


@pytest.fixture(autouse=True)
def _no_exit_prompt(monkeypatch):
    """closeEvent must never block on the keep/discard dialog in tests.

    Answers with the UI default (unchecked = discard). Tests that exercise
    the "keep" outcome re-stub ``exit_dialog.ask_keep_on_exit`` themselves.
    """
    from video_previewer.ui import exit_dialog

    monkeypatch.setattr(
        exit_dialog,
        "ask_keep_on_exit",
        lambda parent, folder, video_count: False,
    )


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
