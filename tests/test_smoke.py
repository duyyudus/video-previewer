"""End-to-end offscreen smoke test of the MVP interaction loop:

    open folder -> grid populates -> thumbnails appear -> hover preview
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from conftest import HAS_FFMPEG, make_video, pump
from video_previewer.ui.main_window import MainWindow

pytestmark = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not available")


@pytest.fixture
def video_folder(tmp_path):
    folder = tmp_path / "vids"
    (folder / "sub").mkdir(parents=True)
    make_video(folder / "alpha.mp4", seconds=5)
    make_video(folder / "beta.webm", seconds=5, vcodec="libvpx", acodec="libopus")
    make_video(folder / "sub" / "gamma.mp4", seconds=5)
    (folder / "notes.txt").write_text("not a video")
    (folder / "ignored.avi.bak").write_text("wrong extension")
    return folder


def test_open_folder_populates_grid_and_thumbnails(qapp, cache_dir, video_folder):
    win = MainWindow()
    win.show()
    try:
        win._open_folder(video_folder)

        # Non-recursive scan: 2 videos appear (grid populates incrementally).
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30), \
            f"model has {win.model.count()} items"

        # Thumbnails + metadata finish asynchronously.
        def thumbs_ready():
            if win.model.count() < 2:
                return False
            return all(
                win.model.item_at(i).thumb_ready for i in range(win.model.count())
            )

        assert pump(qapp, thumbs_ready, timeout=60), "thumbnails did not finish"

        # Metadata came from ffprobe.
        for i in range(win.model.count()):
            item = win.model.item_at(i)
            assert item.duration_ms and 4000 <= item.duration_ms <= 6500
            assert item.width == 320 and item.height == 180
            # Thumbnail file is a decodable 320px JPEG.
            img = QImage()
            assert img.load(str(item.thumbnail_path)), f"bad thumbnail {item.thumbnail_path}"
            assert img.width() == 320

        # Hover API drives the shared player (decoder itself is platform-dependent).
        item = win.model.item_at(0)
        rect = win.grid._cell_rect(0)
        win.player.enter(str(item.path), rect)
        assert win.player.active_path == str(item.path)
        win.player.scrub(0.25)
        win.player.leave()
        assert win.player.active_path is None

        # Switching to recursive picks up the subfolder video.
        win._recursive_chk.setChecked(True)
        assert pump(qapp, lambda: win.model.count() == 3, timeout=30), \
            f"recursive scan found {win.model.count()}"
    finally:
        win.close()


def test_reopen_folder_uses_scan_cache(qapp, cache_dir, video_folder):
    win = MainWindow()
    try:
        win._open_folder(video_folder)
        # The scan cache is written before the last item batch is emitted,
        # so once the grid is populated the cache exists on disk.
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)

        win2 = MainWindow()
        try:
            win2._open_folder(video_folder)
            # Cached items are emitted before the fresh walk finishes, so the
            # grid should populate quickly (5 s budget = plenty if cached).
            assert pump(qapp, lambda: win2.model.count() == 2, timeout=5)
        finally:
            win2.close()
    finally:
        win.close()


def test_grid_virtualization_and_layout(qapp, cache_dir, video_folder):
    from PySide6.QtCore import QSize

    win = MainWindow()
    win.show()
    try:
        win._open_folder(video_folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        qapp.processEvents()

        grid = win.grid
        assert grid.gridSize().width() > 100
        assert grid.gridSize().height() > 100

        # Widening the window keeps the cell size sane (responsive columns).
        grid.resize(QSize(1600, 900))
        qapp.processEvents()
        w = grid.gridSize().width()
        assert 100 <= w <= 500
    finally:
        win.close()
