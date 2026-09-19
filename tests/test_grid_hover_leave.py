"""Viewport Leave handling: phantom Leaves (macOS native video surface)
must not tear down an active hover preview; only a Leave with the real
cursor actually outside the viewport may clear it.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from video_previewer.models.video_item import VideoItem
from video_previewer.models.video_model import VideoModel
from video_previewer.ui import video_grid as video_grid_mod
from video_previewer.ui.video_grid import VideoGrid


class _StubPlayer:
    """Records the hover API calls (duck-typed PreviewPlayer stand-in)."""

    def __init__(self) -> None:
        self.entered: list[str] = []
        self.left = 0

    def enter(self, path: str, rect) -> None:
        self.entered.append(path)

    def leave(self) -> None:
        self.left += 1

    def scrub(self, fraction: float) -> None:
        pass

    def reposition(self, rect) -> None:
        pass


class _FakeCursor:
    """Stand-in for QCursor with a scriptable position."""

    def __init__(self) -> None:
        self._pos = QPoint(0, 0)

    def pos(self) -> QPoint:
        return self._pos


def _make_grid(qapp, tmp_path: Path) -> VideoGrid:
    model = VideoModel()
    model.add_items(
        [VideoItem(tmp_path / f"v{i}.mp4", 1000, 100.0) for i in range(4)]
    )
    grid = VideoGrid(model)
    grid.resize(600, 400)
    grid.show()
    qapp.processEvents()
    return grid


def _move(viewport, pos: QPoint) -> None:
    ev = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(pos),
        QPointF(viewport.mapToGlobal(pos)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(viewport, ev)


def _leave(viewport) -> None:
    QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))


def test_hover_started_on_move(qapp, tmp_path, monkeypatch):
    cursor = _FakeCursor()
    monkeypatch.setattr(video_grid_mod, "QCursor", cursor)
    grid = _make_grid(qapp, tmp_path)
    player = _StubPlayer()
    grid.set_player(player)
    try:
        cell = grid._cell_rect(0)
        _move(grid.viewport(), cell.center())
        assert player.entered and grid._hover_row == 0
    finally:
        grid.deleteLater()


def test_phantom_leave_inside_viewport_keeps_hover(qapp, tmp_path, monkeypatch):
    """macOS delivers a Leave when the native video surface appears under
    the cursor; the pointer is still inside, so hover must survive."""
    cursor = _FakeCursor()
    monkeypatch.setattr(video_grid_mod, "QCursor", cursor)
    grid = _make_grid(qapp, tmp_path)
    player = _StubPlayer()
    grid.set_player(player)
    try:
        vp = grid.viewport()
        cell = grid._cell_rect(0)
        _move(vp, cell.center())
        assert grid._hover_row == 0

        # Real cursor still inside the viewport -> Leave must be ignored.
        cursor._pos = vp.mapToGlobal(vp.rect().center())
        _leave(vp)
        assert player.left == 0
        assert grid._hover_row == 0
    finally:
        grid.deleteLater()


def test_real_leave_outside_viewport_clears_hover(qapp, tmp_path, monkeypatch):
    cursor = _FakeCursor()
    monkeypatch.setattr(video_grid_mod, "QCursor", cursor)
    grid = _make_grid(qapp, tmp_path)
    player = _StubPlayer()
    grid.set_player(player)
    try:
        vp = grid.viewport()
        cell = grid._cell_rect(0)
        _move(vp, cell.center())
        assert grid._hover_row == 0

        # Cursor genuinely outside the viewport -> Leave clears the hover.
        cursor._pos = grid.mapToGlobal(QPoint(grid.width() + 50, grid.height() + 50))
        _leave(vp)
        assert player.left == 1
        assert grid._hover_row == -1
    finally:
        grid.deleteLater()