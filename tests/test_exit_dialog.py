"""Independent folder-restoration and cache choices on close.

The conftest ``_no_exit_prompt`` stub uses the UI defaults so no test blocks
on a modal; tests exercising other combinations re-stub the choice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QLabel

from conftest import make_video, pump
from video_previewer import config
from video_previewer.cache.database import Database
from video_previewer.ui import exit_dialog
from video_previewer.ui.main_window import MainWindow


@pytest.fixture
def video_folder(tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    make_video(folder / "alpha.mp4", seconds=2)
    make_video(folder / "beta.mp4", seconds=2)
    return folder


@pytest.fixture
def db(cache_dir):
    d = Database(cache_dir / "metadata.sqlite")
    yield d
    d.close()


def _read_settings(path: str) -> QSettings:
    return QSettings(path, QSettings.Format.IniFormat)


def _thumbs_ready(win) -> bool:
    if win.model.count() < 2:
        return False
    return all(win.model.item_at(i).thumb_ready for i in range(win.model.count()))


def test_exit_dialog_defaults_to_forget_folder_but_keep_cache(qapp, video_folder):
    dlg, remember_folder, keep_cache = exit_dialog.build_dialog(
        None, video_folder, 2
    )
    try:
        assert remember_folder.isChecked() is False
        assert keep_cache.isChecked() is True
        text = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
        assert str(video_folder) in text
        assert "2 videos" in text
    finally:
        dlg.deleteLater()


def test_close_forgets_folder_but_keeps_cached_videos_by_default(
    qapp, cache_dir, video_folder, db, file_settings
):
    win = MainWindow()
    win.show()
    try:
        win._open_folder(video_folder)
        assert pump(qapp, lambda: _thumbs_ready(win), timeout=60), \
            "grid/thumbnails unfinished"
        win.close()
    finally:
        win.deleteLater()

    # The folder is not remembered...
    s = _read_settings(file_settings)
    assert s.value(config.SETTING_LAST_FOLDER) is None
    assert s.value(config.SETTING_RECURSIVE) is None
    # ...but its reusable cache survives independently.
    prefix = video_folder.absolute().as_posix()
    rows = db.videos_under(prefix)
    assert len(rows) == 2
    for row in rows:
        assert Path(row.thumbnail).exists()
    assert db.load_scan(db.scan_key(video_folder, False)) is not None
    assert db.load_scan(db.scan_key(video_folder, True)) is None


def test_close_keeps_folder_when_checked(
    qapp, cache_dir, video_folder, db, file_settings, monkeypatch
):
    monkeypatch.setattr(
        exit_dialog,
        "ask_exit_choices",
        lambda parent, folder, n: exit_dialog.ExitChoices(True, True),
    )
    win = MainWindow()
    win.show()
    try:
        win._open_folder(video_folder)
        assert pump(qapp, lambda: _thumbs_ready(win), timeout=60), \
            "grid/thumbnails unfinished"
        win.close()  # stub answers "keep"
    finally:
        win.deleteLater()

    s = _read_settings(file_settings)
    assert s.value(config.SETTING_LAST_FOLDER) == str(video_folder)
    rows = db.videos_under(video_folder.absolute().as_posix())
    assert len(rows) == 2
    for row in rows:  # cached thumbnails still on disk
        assert Path(row.thumbnail).exists()
    assert db.load_scan(db.scan_key(video_folder, False)) is not None


def test_close_can_discard_cache_without_remembering_folder(
    qapp, cache_dir, video_folder, db, file_settings, monkeypatch
):
    monkeypatch.setattr(
        exit_dialog,
        "ask_exit_choices",
        lambda parent, folder, n: exit_dialog.ExitChoices(False, False),
    )
    win = MainWindow()
    win.show()
    try:
        win._open_folder(video_folder)
        assert pump(qapp, lambda: _thumbs_ready(win), timeout=60), \
            "grid/thumbnails unfinished"
        win.close()
    finally:
        win.deleteLater()

    s = _read_settings(file_settings)
    assert s.value(config.SETTING_LAST_FOLDER) is None
    assert s.value(config.SETTING_RECURSIVE) is None
    assert db.videos_under(video_folder.absolute().as_posix()) == []
    assert db.load_scan(db.scan_key(video_folder, False)) is None
