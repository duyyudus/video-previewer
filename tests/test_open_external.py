"""Opening a video with the OS-default player (double-click on a tile)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent

from video_previewer.models.video_item import VideoItem
from video_previewer.models.video_model import VideoModel
from video_previewer.open_external import open_externally
from video_previewer.ui.video_grid import VideoGrid


def _make_item(folder: Path, name: str = "alpha.mp4") -> VideoItem:
    path = folder / name
    path.write_bytes(b"not really a video, but the tests never play it")
    return VideoItem(path=path, size=path.stat().st_size, modified=path.stat().st_mtime)


def _mouse_event(etype: QEvent.Type, pos: QPointF) -> QMouseEvent:
    return QMouseEvent(
        etype,
        pos,
        pos,
        Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def _press(grid: VideoGrid, pos: QPointF) -> None:
    """Deliver a left press the way the viewport event filter sees it."""
    grid.eventFilter(grid.viewport(), _mouse_event(QEvent.Type.MouseButtonPress, pos))


# -- open_externally dispatch ------------------------------------------------------


def test_open_externally_windows_uses_startfile(monkeypatch, tmp_path):
    import os

    calls = []

    def fake_startfile(path):
        calls.append(path)

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(os, "startfile", fake_startfile, raising=False)
    assert open_externally(tmp_path / "a.mp4") is True
    assert calls == [str(tmp_path / "a.mp4")]


@pytest.mark.parametrize("platform_cmd", [("darwin", "open"), ("linux", "xdg-open")])
def test_open_externally_unix_uses_launcher(monkeypatch, tmp_path, platform_cmd):
    import video_previewer.open_external as mod

    platform, cmd = platform_cmd
    calls = []

    class FakePopen:
        def __init__(self, argv):
            calls.append(argv)

    monkeypatch.setattr("sys.platform", platform)
    monkeypatch.setattr(mod.subprocess, "Popen", FakePopen)
    assert open_externally(tmp_path / "a.mp4") is True
    assert calls == [[cmd, str(tmp_path / "a.mp4")]]


def test_open_externally_fails_soft(monkeypatch, tmp_path):
    import os

    def boom(path):
        raise OSError("no association")

    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(os, "startfile", boom, raising=False)
    assert open_externally(tmp_path / "a.mp4") is False


# -- grid double-click ---------------------------------------------------------------


class _StubPlayer:
    def __init__(self):
        self.left = False

    def leave(self):
        self.left = True


def _double_click(grid: VideoGrid, pos: QPointF) -> None:
    grid.mouseDoubleClickEvent(_mouse_event(QEvent.Type.MouseButtonDblClick, pos))


def test_double_click_opens_item_with_os_player(monkeypatch, qapp, tmp_path):
    model = VideoModel()
    item = _make_item(tmp_path)
    model.add_items([item])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )

    rect = grid.visualRect(model.index(0))
    assert rect.isValid()
    _double_click(grid, QPointF(rect.center()))
    assert opened == [item.path]


def test_double_click_stops_hover_preview(monkeypatch, qapp, tmp_path):
    model = VideoModel()
    item = _make_item(tmp_path)
    model.add_items([item])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    stub = _StubPlayer()
    grid.set_player(stub)  # type: ignore[arg-type]
    grid._hover_row = 0

    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally", lambda path: True
    )
    rect = grid.visualRect(model.index(0))
    _double_click(grid, QPointF(rect.center()))
    assert stub.left
    assert grid._hover_row == -1


def test_double_click_on_empty_space_requests_folder_open(monkeypatch, qapp, tmp_path):
    """Empty grid space (no video under the pointer) offers the folder picker."""
    model = VideoModel()
    model.add_items([_make_item(tmp_path)])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    requests = []
    grid.open_folder_requested.connect(lambda: requests.append(1))
    # Far below/right of the single tile: empty grid space.
    _double_click(grid, QPointF(700, 550))
    assert opened == []  # no video is opened...
    assert requests == [1]  # ...but the folder-open action is requested


def test_two_quick_presses_on_empty_space_request_folder_open(
    monkeypatch, qapp, tmp_path
):
    """The press-based fallback must also cover empty grid space."""
    model = VideoModel()
    model.add_items([_make_item(tmp_path)])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    requests = []
    grid.open_folder_requested.connect(lambda: requests.append(1))
    _press(grid, QPointF(700, 550))
    time.sleep(0.05)
    _press(grid, QPointF(702, 550))
    assert requests == [1]
    assert opened == []


def test_triple_click_on_empty_space_requests_once(monkeypatch, qapp, tmp_path):
    """A triple click delivers MouseButtonDblClick twice: request once."""
    model = VideoModel()
    model.add_items([_make_item(tmp_path)])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    requests = []
    grid.open_folder_requested.connect(lambda: requests.append(1))
    _double_click(grid, QPointF(700, 550))
    _double_click(grid, QPointF(700, 550))
    assert requests == [1]
    assert opened == []


def test_presses_straddling_tile_and_empty_space_do_nothing(
    monkeypatch, qapp, tmp_path
):
    """A press pair must land on the same kind of target (tile or empty)."""
    model = VideoModel()
    model.add_items([_make_item(tmp_path)])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    requests = []
    grid.open_folder_requested.connect(lambda: requests.append(1))
    rect = grid.visualRect(model.index(0))
    center = QPointF(rect.center())
    empty = QPointF(700, 550)
    _press(grid, center)
    _press(grid, empty)  # tile -> empty: neither gesture completes
    assert opened == []
    assert requests == []
    time.sleep((grid._dbl_interval_ms() + 100) / 1000)  # start a fresh gesture
    _press(grid, empty)
    _press(grid, center)  # empty -> tile: ditto
    assert opened == []
    assert requests == []


def test_two_quick_presses_open_without_qt_dblclick(monkeypatch, qapp, tmp_path):
    """Qt can miss the DblClick synthesis (native video widget churn); the
    press-based fallback must open the item anyway."""
    model = VideoModel()
    item = _make_item(tmp_path)
    model.add_items([item])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    rect = grid.visualRect(model.index(0))
    center = QPointF(rect.center())
    _press(grid, center)
    time.sleep(0.05)
    _press(grid, center + QPointF(2, 0))
    assert opened == [item.path]


def test_two_slow_presses_do_not_open(monkeypatch, qapp, tmp_path):
    model = VideoModel()
    model.add_items([_make_item(tmp_path)])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    rect = grid.visualRect(model.index(0))
    _press(grid, QPointF(rect.center()))
    time.sleep((grid._dbl_interval_ms() + 100) / 1000)
    _press(grid, QPointF(rect.center()))
    assert opened == []


def test_triple_click_opens_only_once(monkeypatch, qapp, tmp_path):
    """A triple click delivers MouseButtonDblClick twice: open once."""
    model = VideoModel()
    model.add_items([_make_item(tmp_path)])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    item = model.item_at(0)
    assert item is not None
    rect = grid.visualRect(model.index(0))
    center = QPointF(rect.center())
    _double_click(grid, center)
    _double_click(grid, center)
    assert opened == [item.path]


def test_quick_presses_on_different_tiles_do_not_open(monkeypatch, qapp, tmp_path):
    model = VideoModel()
    item_a = _make_item(tmp_path, "a.mp4")
    item_b = _make_item(tmp_path, "b.mp4")
    model.add_items([item_a, item_b])
    grid = VideoGrid(model)
    grid.resize(800, 600)
    grid.show()
    qapp.processEvents()

    opened = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally",
        lambda path: opened.append(path) or True,
    )
    rect_a = grid.visualRect(model.index(0))
    rect_b = grid.visualRect(model.index(1))
    assert rect_b.isValid() and not rect_b.intersects(rect_a)
    _press(grid, QPointF(rect_a.center()))
    _press(grid, QPointF(rect_b.center()))
    assert opened == []
