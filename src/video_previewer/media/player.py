"""Shared hover-preview player.

Architectural rule: exactly ONE ``QMediaPlayer`` + ONE video output widget
exist for the whole app. Hovering a tile moves/resizes that single widget
over the tile and starts muted playback; leaving stops it. Scrubbing is
throttled (a single pending seek flushed at most ~30x/s).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QRect, Qt, QUrl, QTimer
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QWidget

from .. import config
from .seek_bar import SeekBarOverlay

log = logging.getLogger(__name__)

#: Mirror of VideoGrid._DEBUG_POINTER (VIDEO_PREVIEWER_DEBUG_POINTER=1).
_DEBUG_POINTER = bool(os.environ.get("VIDEO_PREVIEWER_DEBUG_POINTER"))


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
        # While the video surface is visible, macOS delivers pointer events
        # starting AT this widget (transparency notwithstanding). Without
        # tracking, QApplication::notify discards button-less moves here
        # ("throw away any mouse-tracking-only mouse events") so the grid
        # viewport beneath never sees them and hover scrubbing dies; presses
        # propagate, which is why press-drag scrubbing kept working. With
        # tracking the move is delivered, ignored, and propagates to the
        # viewport like presses already do. Same for the seek-bar child.
        self._video.setMouseTracking(True)
        self._video.hide()

        # Timeline overlay pinned to the bottom edge of the video.
        self._seekbar = SeekBarOverlay(self._video)
        self._seekbar.setMouseTracking(True)  # see _video tracking note
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

        # Media generation: incremented whenever the hovered path changes (and
        # on leave). ``_source_gen`` records which generation the player's
        # *current* source belongs to (set in ``_start_playback``). Stale
        # media-status / duration / seek signals from a previous source are
        # dropped while ``_source_gen != _media_gen``, so a slow decoder from
        # the last tile can never pop the video widget over the new tile or
        # seek against a stale duration.
        self._media_gen = 0
        self._source_gen = -1

    # -- hover API (called from the main thread) ----------------------------

    def enter(self, path: str, rect: QRect) -> None:
        """Pointer entered the tile for *path* at viewport rect *rect*."""
        if _DEBUG_POINTER:
            log.info("player enter %s rect=%s cur=%s", Path(path).name, rect,
                     Path(self._path).name if self._path else None)
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
        self._media_gen += 1  # invalidate any in-flight signals from the old source
        self._seekbar.set_progress(0.0)
        if self._video.isVisible():
            self._video.setGeometry(rect)
        self._autoplay.start()

    def leave(self) -> None:
        """Pointer left the hovered tile (or the grid)."""
        if _DEBUG_POINTER and self._path is not None:
            log.info("player leave (was %s)", Path(self._path).name)
        self._autoplay.stop()
        self._pending_seek_ms = None
        self._pending_fraction = None
        self._path = None
        self._media_gen += 1  # stale source signals must not resurrect the widget
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
        if _DEBUG_POINTER:
            log.info("player scrub(%.2f) ready=%s", fraction,
                     self._source_gen == self._media_gen)
        if self._source_gen != self._media_gen:
            # Autoplay not fired yet (or the source is the previous tile's);
            # the player's duration is stale. Remember the fraction so it is
            # applied once the new source's duration arrives.
            self._pending_fraction = max(0.0, min(1.0, fraction))
            self._seekbar.set_progress(self._pending_fraction)
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
        if _DEBUG_POINTER:
            log.info("player autoplay fires: %s", Path(self._path).name)
        self._player.setSource(QUrl.fromLocalFile(self._path))
        self._source_gen = self._media_gen  # signals now belong to this media
        self._player.play()

    def _stop_playback(self) -> None:
        if _DEBUG_POINTER and self._video.isVisible():
            log.info("player hide video widget")
        self._seek.stop()
        self._pending_seek_ms = None
        self._player.stop()
        self._seekbar.hide()
        self._video.hide()
        # Release the file itself, not just playback. Windows keeps the
        # previewed video locked ("the process cannot access the file
        # because it is being used by another process") for as long as a
        # source is set; ``stop()`` alone leaves the media loaded and the
        # handle open, so renaming the file, moving it into a sidebar
        # folder, or letting Explorer finish a drag-out move all fail with
        # a sharing violation. Clearing the source ceases all I/O on the
        # media and drops the handle synchronously; the next hover reloads
        # it anyway.
        self._player.setSource(QUrl())
        self._source_gen = -1  # nothing loaded: in-flight signals are stale

    def _apply_seek(self) -> None:
        ms = self._pending_seek_ms
        self._pending_seek_ms = None
        if self._source_gen != self._media_gen:
            return  # media changed since the scrub; drop the seek
        if ms is None or self._player.duration() <= 0:
            return
        self._player.setPosition(ms)

    def _on_duration_changed(self, duration: int) -> None:
        # A scrub happened while the decoder was warming up: land it now.
        if self._source_gen != self._media_gen:
            return  # stale source; its duration is not the current media's
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
        if self._source_gen != self._media_gen:
            return  # a previous source's status; ignore it
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            if self._path is not None and self._rect is not None and not self._video.isVisible():
                if _DEBUG_POINTER:
                    log.info("player show video widget rect=%s", self._rect)
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
        if self._source_gen != self._media_gen:
            return  # a stale source errored; don't tear down the new preview
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
    def media_source(self) -> QUrl:
        """The URL loaded in the shared player (empty once released)."""
        return self._player.source()

    @property
    def video_widget(self) -> QVideoWidget:
        return self._video

    @property
    def seekbar(self) -> SeekBarOverlay:
        return self._seekbar
