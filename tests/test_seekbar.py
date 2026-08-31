"""SeekBarOverlay: clamping, rendering, and player wiring."""

from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtWidgets import QWidget

from video_previewer.media.player import PreviewPlayer
from video_previewer.media.seek_bar import SeekBarOverlay


def test_progress_clamped(qapp):
    host = QWidget()
    bar = SeekBarOverlay(host)
    bar.set_progress(2.0)
    assert bar.progress == 1.0
    bar.set_progress(-1.0)
    assert bar.progress == 0.0
    bar.set_progress(0.25)
    assert bar.progress == 0.25
    # equal value -> no change
    bar.set_progress(0.25)
    assert bar.progress == 0.25
    host.deleteLater()


def test_paint_fill_track_knob(qapp):
    host = QWidget()
    host.resize(300, 200)
    host.show()
    bar = SeekBarOverlay(host)
    bar.setGeometry(0, 190, 300, 10)
    bar.show()
    bar.set_progress(0.5)
    qapp.processEvents()

    img = bar.grab().toImage()
    assert img.width() == 300 and img.height() == 10

    def rgba(x: int, y: int) -> tuple[int, int, int, int]:
        raw = img.pixel(x, y)
        a = (raw >> 24) & 0xFF
        if a == 0:
            return 0, 0, 0, 0
        # grab() yields premultiplied ARGB32; un-premultiply for assertions
        return (
            min(255, ((raw >> 16) & 0xFF) * 255 // a),
            min(255, ((raw >> 8) & 0xFF) * 255 // a),
            min(255, (raw & 0xFF) * 255 // a),
            a,
        )

    # Background outside the bar is unpainted (transparent)
    r, g, b, a = rgba(290, 0)
    assert a == 0

    # Left of center: bright near-opaque fill
    r, g, b, a = rgba(10, 5)
    assert a > 200 and r > 200 and g > 200

    # Right of center: dark translucent track
    r, g, b, a = rgba(290, 5)
    assert 60 < a < 200 and r < 20

    # Knob at center: opaque white
    r, g, b, a = rgba(150, 5)
    assert a > 250 and r > 240 and g > 240 and b > 240
    host.deleteLater()


def test_follows_parent_resize(qapp):
    host = QWidget()
    host.resize(300, 200)
    host.show()
    bar = SeekBarOverlay(host)
    bar.show()
    qapp.processEvents()
    # initial pinning happens on the first parent resize event
    host.resize(420, 200)
    qapp.processEvents()
    assert bar.x() == 0
    assert bar.width() == 420
    assert bar.y() == 200 - bar.height()


def test_player_position_updates_seekbar(qapp):
    host = QWidget()
    host.resize(300, 200)
    host.show()
    player = PreviewPlayer(host)
    try:
        player.enter(r"C:\v\x.mp4", QRect(0, 0, 300, 200))

        # A reported playback position drives the overlay.
        player._on_position_changed(2500, 10_000)
        assert abs(player.seekbar.progress - 0.25) < 0.001

        # Scrubbing updates the overlay immediately (media not loaded yet,
        # so duration()==0 and the fraction is remembered).
        player.scrub(0.8)
        assert abs(player.seekbar.progress - 0.8) < 0.001

        # Scrubs before any hover are ignored.
        player.leave()
        player.scrub(0.5)
        assert abs(player.seekbar.progress - 0.8) < 0.001  # unchanged
    finally:
        player.deleteLater()
        host.deleteLater()
