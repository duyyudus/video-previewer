"""Settings panel and cache-maintenance behavior."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QTabWidget

from conftest import pump
from video_previewer.models.video_item import VideoItem
from video_previewer.ui import settings_dialog as settings_dialog_mod
from video_previewer.ui.main_window import MainWindow
from video_previewer.ui.settings_dialog import format_byte_size


def test_format_byte_size() -> None:
    assert format_byte_size(0) == "0 B"
    assert format_byte_size(1024) == "1.0 KB"
    assert format_byte_size(1572864) == "1.5 MB"


def test_settings_button_precedes_open_folder_and_opens_cache_tab(
    qapp, cache_dir
):
    win = MainWindow()
    win.show()
    try:
        toolbar = win.centralWidget().layout().itemAt(0).layout()
        assert toolbar.indexOf(win._settings_btn) < toolbar.indexOf(win._browse_btn)

        QTest.mouseClick(win._settings_btn, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        dialog = win._settings_dialog
        assert dialog is not None
        tabs = dialog.findChild(QTabWidget, "settingsTabs")
        assert tabs is not None
        assert tabs.tabText(0) == "Cache"
        assert dialog.findChild(QPushButton, "cacheLocationButton").text() == (
            "Cache Location"
        )
        assert dialog.findChild(QPushButton, "clearCacheButton").text() == (
            "Clear Cache"
        )
        assert pump(
            qapp,
            lambda: "Calculating" not in dialog._size_label.text(),
            timeout=2,
        )
    finally:
        win.close()


def test_cache_location_button_opens_cache_folder(
    qapp, cache_dir, monkeypatch
):
    opened: list[Path] = []
    monkeypatch.setattr(settings_dialog_mod, "open_externally", opened.append)
    win = MainWindow()
    win.show()
    try:
        win._show_settings()
        dialog = win._settings_dialog
        assert dialog is not None
        dialog.findChild(QPushButton, "cacheLocationButton").click()
        assert opened == [cache_dir]
    finally:
        win.close()


def test_clear_cache_removes_thumbnails_metadata_and_scans(
    qapp, cache_dir, tmp_path
):
    win = MainWindow()
    win.show()
    try:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"video")
        stat = video.stat()
        item = VideoItem(video, stat.st_size, stat.st_mtime)
        win._cache.store_failed(item)
        scan_key = win._db.scan_key(tmp_path, False)
        win._db.save_scan(scan_key, [{"path": video.as_posix()}])
        thumbnail = win._cache.thumbnail_path_for(item.vid)
        thumbnail.write_bytes(b"jpeg")

        win._show_settings()
        dialog = win._settings_dialog
        assert dialog is not None
        dialog.findChild(QPushButton, "clearCacheButton").click()
        assert pump(
            qapp,
            lambda: not win._cache_clear_in_progress,
            timeout=5,
        )

        assert not thumbnail.exists()
        assert win._db.get_video(item.vid) is None
        assert win._db.load_scan(scan_key) is None
        assert dialog.findChild(QPushButton, "clearCacheButton").isEnabled()
        assert pump(
            qapp,
            lambda: "Calculating" not in dialog._size_label.text(),
            timeout=2,
        )
    finally:
        win.close()
