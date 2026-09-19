"""PreviewPlayer: seek math + offscreen lifecycle (no decoder assertions)."""

from __future__ import annotations

from PySide6.QtCore import QRect

from video_previewer.media.player import PreviewPlayer, seek_ms


def test_seek_ms_math():
    assert seek_ms(0.0, 10_000) == 0
    assert seek_ms(1.0, 10_000) == 10_000
    assert seek_ms(0.5, 9_999) == 4_999
    # clamped to [0, duration]
    assert seek_ms(-0.5, 10_000) == 0
    assert seek_ms(1.5, 10_000) == 10_000


def test_player_lifecycle_offscreen(qapp):
    from PySide6.QtWidgets import QWidget

    host = QWidget()
    player = PreviewPlayer(host)
    try:
        rect = QRect(10, 10, 220, 154)
        # enter schedules autoplay (200 ms); no real media needed
        player.enter(r"C:\does\not\exist.mp4", rect)
        assert player.active_path == r"C:\does\not\exist.mp4"
        # re-entering the same tile just repositions
        player.enter(r"C:\does\not\exist.mp4", rect.adjusted(5, 5, -5, -5))
        assert player.active_path == r"C:\does\not\exist.mp4"

        # scrubbing without a loaded duration is a safe no-op
        player.scrub(0.3)

        # leaving stops everything and hides the output
        player.leave()
        assert player.active_path is None
        assert not player.video_widget.isVisible()

        # let any queued autoplay/error signals run; nothing may crash
        qapp.processEvents()
    finally:
        player.deleteLater()
        host.deleteLater()


def test_move_propagates_from_video_surface(qapp):
    """Button-less moves hitting the visible video widget must propagate
    to the tracking host viewport beneath it (macOS starts pointer events
    at the video surface; without mouse tracking Qt would discard them
    there and hover scrubbing would die)."""
    from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QWidget

    host = QWidget()
    host.resize(300, 200)
    host.setMouseTracking(True)  # like the grid viewport
    host.show()
    player = PreviewPlayer(host)
    try:
        vw = player.video_widget
        vw.setGeometry(50, 40, 200, 100)
        vw.show()
        qapp.processEvents()
        assert vw.hasMouseTracking()
        assert player.seekbar.hasMouseTracking()

        seen: list[QPoint] = []

        class _Filter(QObject):
            def eventFilter(self, obj, event):  # noqa: N802
                if event.type() == QEvent.Type.MouseMove:
                    seen.append(event.position().toPoint())
                return False

        f = _Filter()
        host.installEventFilter(f)

        ev = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(10, 10),
            QPointF(10, 10),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(vw, ev)
        # Local (10,10) inside the widget at (50,40) -> host (60,50),
        # arriving exactly once via parent propagation.
        assert seen == [QPoint(60, 50)]

        # A move over the seek-bar child propagates through two levels.
        seen.clear()
        sb = player.seekbar
        QApplication.sendEvent(
            sb,
            QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(5, 5),
                QPointF(5, 5),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
        assert seen == [QPoint(55, 40 + sb.y() + 5)]
    finally:
        player.deleteLater()
        host.deleteLater()
