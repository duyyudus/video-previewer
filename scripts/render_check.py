"""Throwaway visual check: run the real app offscreen and grab a screenshot.

Generates a handful of test videos, opens them in the main window, waits
for thumbnails, then saves a PNG of the rendered grid.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtWidgets import QApplication  # noqa: E402
from PySide6.QtGui import QPalette, QColor  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent / "src"))

from video_previewer import app as app_mod  # noqa: E402
from video_previewer.ui.main_window import MainWindow  # noqa: E402
from video_previewer import config  # noqa: E402


def make_video(path: Path, seconds: int, hue: int) -> None:
    if path.suffix == ".webm":
        vcodec, acodec = "libvpx-vp9", "libopus"
    else:
        vcodec, acodec = "libx264", "aac"
    subprocess.run(
        [
            config.ffmpeg_path(),
            "-v", "error", "-y",
            "-f", "lavfi",
            "-i", f"testsrc2=duration={seconds}:size=640x360:rate=24",
            "-f", "lavfi", "-i", f"sine=frequency={220 + hue}:duration={seconds}",
            "-c:v", vcodec, "-preset", "ultrafast",
            "-c:a", acodec, "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="vp-preview-"))
    cache = tmp / "cache"
    os.environ["VIDEO_PREVIEWER_CACHE_DIR"] = str(cache)

    vid = tmp / "videos"
    vid.mkdir()
    names = ["alpha.mp4", "beta.mkv", "gamma.mov", "delta.webm", "epsilon.m4v",
             "zeta.avi", "eta.mp4", "theta.mp4", "iota.mp4", "kappa.mp4"]
    for i, name in enumerate(names):
        make_video(vid / name, 6, i)
    print(f"generated {len(names)} videos in {vid}")

    app = QApplication(sys.argv)
    app.setApplicationName("video-previewer")
    app.setOrganizationName(config.ORG_NAME)
    app_mod._apply_dark_palette(app)

    win = MainWindow()
    win.resize(1440, 900)
    win.show()
    win._open_folder(vid)

    deadline = time.time() + 60
    while time.time() < deadline:
        app.processEvents()
        if win.model.count() == len(names) and all(
            win.model.item_at(i).thumb_ready for i in range(win.model.count())
        ):
            break
        time.sleep(0.02)

    ready = sum(win.model.item_at(i).thumb_ready for i in range(win.model.count()))
    print(f"model: {win.model.count()} items, {ready} thumbnails ready")
    if ready < len(names):
        print("WARNING: not all thumbnails ready in time")

    time.sleep(0.3)
    app.processEvents()
    out = Path(__file__).parent / "screenshot-offscreen.png"
    ok = win.grab().save(str(out))
    print(f"screenshot saved: {out} ({ok})")
    win.close()


if __name__ == "__main__":
    main()
