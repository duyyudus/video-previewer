"""Shared hover-preview player.

Architectural rule: exactly ONE ``QMediaPlayer`` + ONE video output widget
exist for the whole app. Hovering a tile moves/resizes that single widget
over the tile and starts muted playback; leaving stops it. Scrubbing is
throttled (a single pending seek flushed at most ~30x/s).
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRect, QUrl, QTimer
from PySide6.QtGui import Qt
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QWidget

from .. import config
from .seek_bar import SeekBarOverlay

log = logging.getLogger(__name__)


def seek_ms(fraction: float, duration_ms: int) -> int:
    """Map a 0..1 horizontal pointer fraction to a seek position in ms."""
    return int(max(0.0, min(1.0, fraction)) * duration_ms)


class PreviewPlayer(QObject):
    """One shared player driving all hover previews."""

    def __init__(self, host: QWidget, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._host = host

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self._player)
        self._audio.setVolume(0.0)
        self._audio.setMuted(True)

        self._video = QVideoWidget(host)
        self._video.setAspectRatioMode(
            Qt.AspectRatioMode.KeepAspectRatioByExpanding  # fill + center crop
        )
        self._video.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._video.hide()

        # Timeline overlay pinned to the bottom edge of the video.
        self._seekbar = SeekBarOverlay(self._video)
        self._seekbar.hide()

        self._player.setAudioOutput(self._audio)
        self._player.setVideoOutput(self._video)
        self._player.setLoops(0)  # loop while the pointer stays on the tile
        self._player.errorOccurred.connect(self._on_error)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._player.durationChanged.connect(self._on_duration_changed)
        self._player.positionChanged.connect(self._on_position_changed)

        # Autoplay delay: avoids decoder churn when the pointer sweeps the grid.
        self._autoplay = QTimer(self)
        self._autoplay.setSingleShot(True)
        self._autoplay.setInterval(config.AUTOPLAY_DELAY_MS)
        self._autoplay.timeout.connect(self._start_playback)

        # Seek throttle: at most one seek per interval.
        self._seek = QTimer(self)
        self._seek.setSingleShot(True)
        self._seek.setInterval(config.SEEK_THROTTLE_MS)
        self._seek.timeout.connect(self._apply_seek)

        self._path: str | None = None
        self._rect: QRect | None = None
        self._pending_seek_ms: int | None = None
        self._pending_fraction: float | None = None

    # -- hover API (called from the main thread) ----------------------------

    def enter(self, path: str, rect: QRect) -> None:
        """Pointer entered the tile for *path* at viewport rect *rect*."""
        self._autoplay.stop()
        self._pending_seek_ms = None
        self._pending_fraction = None
        self._rect = rect
        if self._path == path:
            if self._video.isVisible():
                self._video.setGeometry(rect)
            return
        self._stop_playback()
        self._path = path
        self._seekbar.set_progress(0.0)
        if self._video.isVisible():
            self._video.setGeometry(rect)
        self._autoplay.start()

    def leave(self) -> None:
        """Pointer left the hovered tile (or the grid)."""
        self._autoplay.stop()
        self._pending_seek_ms = None
        self._pending_fraction = None
        self._path = None
        self._stop_playback()

    def reposition(self, rect: QRect) -> None:
        """Called when the grid scrolls/resizes while a preview is active."""
        self._rect = rect
        if self._path is not None and self._video.isVisible():
            self._video.setGeometry(rect)

    def scrub(self, fraction: float) -> None:
        """Map horizontal pointer position to a throttled seek.

        If the media duration is not known yet (decoder warming up), the
        fraction is remembered and applied once the duration arrives.
        """
        if self._path is None:
            return
        duration = self._player.duration()
        if duration <= 0:
            self._pending_fraction = max(0.0, min(1.0, fraction))
            self._seekbar.set_progress(self._pending_fraction)
            return
        ms = seek_ms(fraction, duration)
        self._pending_seek_ms = ms
        # Immediate visual feedback; positionChanged takes over once the
        # decoder actually lands at the new position.
        self._seekbar.set_progress(max(0.0, min(1.0, fraction)))
        if not self._seek.isActive():
            self._seek.start()

    # -- internals -----------------------------------------------------------

    def _start_playback(self) -> None:
        if self._path is None:
            return
        self._player.setSource(QUrl.fromLocalFile(self._path))
        self._player.play()

    def _stop_playback(self) -> None:
        self._seek.stop()
        self._pending_seek_ms = None
        self._player.stop()
        self._seekbar.hide()
        self._video.hide()

    def _apply_seek(self) -> None:
        ms = self._pending_seek_ms
        self._pending_seek_ms = None
        if ms is None or self._player.duration() <= 0:
            return
        self._player.setPosition(ms)

    def _on_duration_changed(self, duration: int) -> None:
        # A scrub happened while the decoder was warming up: land it now.
        if duration <= 0:
            return
        if self._pending_fraction is not None:
            self._pending_seek_ms = seek_ms(self._pending_fraction, duration)
            self._pending_fraction = None
            if not self._seek.isActive():
                self._seek.start()

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        # Show the (blank until first frame) video widget only once media is
        # loaded, so the static thumbnail stays visible while the decoder warms up.
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            if self._path is not None and self._rect is not None and not self._video.isVisible():
                self._video.setGeometry(self._rect)
                self._video.show()
                self._seekbar.show()

    def _on_position_changed(self, position: int, duration: int | None = None) -> None:
        """Follow the real playback position in the timeline overlay.

        (The signal passes only *position*; *duration* is an optional
        override used by tests.)
        """
        if self._path is None:
            return
        duration = duration if duration is not None else self._player.duration()
        if duration <= 0:
            return
        self._seekbar.set_progress(position / duration)

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        # Never crash the app on a bad file: fall back to the static thumbnail.
        # (The Qt FFmpeg category is filtered to warning+ in the console, so
        # surface player-level errors through the Python logger.)
        path = self._path
        self._path = None
        self._stop_playback()
        if path:
            log.warning("preview playback error (%s) for %s: %s", error, path, message)

    # -- test/inspection helpers ----------------------------------------------

    @property
    def active_path(self) -> str | None:
        return self._path

    @property
    def video_widget(self) -> QVideoWidget:
        return self._video

    @property
    def seekbar(self) -> SeekBarOverlay:
        return self._seekbar
