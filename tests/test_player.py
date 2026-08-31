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
