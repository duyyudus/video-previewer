"""Quick access: pin/unpin, persistence, click-to-load, missing folders."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QModelIndex, QSettings, Qt
from PySide6.QtTest import QTest

from conftest import pump
from video_previewer import config
from video_previewer.ui.main_window import MainWindow
from video_previewer.ui.quick_access import QuickAccessList


def _list(qapp) -> QuickAccessList:
    return QuickAccessList(QSettings())


def test_pin_unpin_and_dedupe(qapp, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    qa = _list(qapp)
    try:
        qa.pin(str(a))
        qa.pin(str(b))
        qa.pin(str(a) + "/")  # same folder, different spelling
        assert [Path(p) for p in qa.pins()] == [a, b]
        assert qa.is_pinned(str(b))
        qa.toggle_pin(str(b))
        assert [Path(p) for p in qa.pins()] == [a]
        assert Path(qa.item(0).text()) == a  # full path, not just "a"
    finally:
        qa.deleteLater()
        qapp.processEvents()


def test_pins_persist_in_order(qapp, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    qa = _list(qapp)
    qa.pin(str(b))
    qa.pin(str(a))
    qa.deleteLater()
    again = _list(qapp)
    try:
        assert [Path(p) for p in again.pins()] == [b, a]
    finally:
        again.deleteLater()
        qapp.processEvents()


def test_reorder_is_saved(qapp, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    qa = _list(qapp)
    qa.pin(str(a))
    qa.pin(str(b))
    # What an internal drag-reorder does to the model.
    assert qa.model().moveRows(QModelIndex(), 1, 1, QModelIndex(), 0)
    qa.deleteLater()
    again = _list(qapp)
    try:
        assert [Path(p) for p in again.pins()] == [b, a]
    finally:
        again.deleteLater()
        qapp.processEvents()


def test_corrupt_setting_means_no_pins(qapp):
    QSettings().setValue(config.SETTING_QUICK_ACCESS, "{not json")
    qa = _list(qapp)
    try:
        assert qa.pins() == []
    finally:
        qa.deleteLater()
        qapp.processEvents()


def test_missing_folder_stays_pinned_but_inert(qapp, tmp_path):
    gone = tmp_path / "gone"
    qa = _list(qapp)
    qa.pin(str(gone))
    fired: list[str] = []
    qa.folder_activated.connect(fired.append)
    try:
        qa.show()
        rect = qa.visualItemRect(qa.item(0))
        QTest.mouseClick(qa.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert fired == []
        assert "not found" in qa.item(0).toolTip()
    finally:
        qa.deleteLater()
        qapp.processEvents()


def test_click_loads_pinned_folder(qapp, cache_dir, tmp_path):
    folder = tmp_path / "fav"
    folder.mkdir()
    win = MainWindow()
    try:
        win.show()
        win.sidebar.pin_toggled.emit(str(folder))  # as the tree's context menu does
        qa = win.quick_access
        assert qa.is_pinned(str(folder))
        assert win.sidebar.is_pinned(str(folder))
        rect = qa.visualItemRect(qa.item(0))
        QTest.mouseClick(qa.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
        assert pump(qapp, lambda: win._current_folder == folder)
    finally:
        win.close()
        qapp.processEvents()
