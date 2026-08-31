"""Exit prompt: keep/discard the selected folder and its videos on close.

The conftest ``_no_exit_prompt`` stub answers the dialog with the UI default
(unchecked = discard) so no test blocks on a modal; the keep test re-stubs
``ask_keep_on_exit`` itself.
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


@pytest.fixture
def file_settings(tmp_path, monkeypatch):
    """Point MainWindow's QSettings at a file (INI format).

    The DSH file sandbox silently blocks QSettings persistence in the native
    (registry) format, so the close-behavior tests run against a file-backed
    settings store instead (works everywhere, readable from fresh instances).
    """
    from video_previewer.ui import main_window as main_window_mod

    path = str(tmp_path / "settings.ini")

    class FileSettings(QSettings):
        def __init__(self) -> None:
            super().__init__(path, QSettings.Format.IniFormat)

    monkeypatch.setattr(main_window_mod, "QSettings", FileSettings)
    return path


def _read_settings(path: str) -> QSettings:
    return QSettings(path, QSettings.Format.IniFormat)


def _thumbs_ready(win) -> bool:
    if win.model.count() < 2:
        return False
    return all(win.model.item_at(i).thumb_ready for i in range(win.model.count()))


def test_exit_dialog_defaults_to_discard(qapp, video_folder):
    dlg, remember = exit_dialog.build_dialog(None, video_folder, 2)
    try:
        assert remember.isChecked() is False  # off by default: don't remember
        text = " ".join(lbl.text() for lbl in dlg.findChildren(QLabel))
        assert str(video_folder) in text
        assert "2 videos" in text
    finally:
        dlg.deleteLater()


def test_close_discards_folder_and_videos_by_default(
    qapp, cache_dir, video_folder, db, file_settings
):
    win = MainWindow()
    win.show()
    try:
        win._open_folder(video_folder)
        assert pump(qapp, lambda: _thumbs_ready(win), timeout=60), \
            "grid/thumbnails unfinished"
        win.close()  # conftest stub answers with the default (discard)
    finally:
        win.deleteLater()

    # The folder is not remembered...
    s = _read_settings(file_settings)
    assert s.value(config.SETTING_LAST_FOLDER) is None
    assert s.value(config.SETTING_RECURSIVE) is None
    # ...and none of its cached videos survive.
    prefix = video_folder.absolute().as_posix()
    assert db.videos_under(prefix) == []
    assert db.load_scan(db.scan_key(video_folder, False)) is None
    assert db.load_scan(db.scan_key(video_folder, True)) is None


def test_close_keeps_folder_when_checked(
    qapp, cache_dir, video_folder, db, file_settings, monkeypatch
):
    monkeypatch.setattr(
        exit_dialog, "ask_keep_on_exit", lambda parent, folder, n: True
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
