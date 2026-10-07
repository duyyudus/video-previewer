"""ffmpeg-based extraction of the frames of a video as images.

The frames of ``clip.mkv`` land in a ``clip_frames`` folder — next to the
video, or inside a custom root folder — as ``clip_0001.png``,
``clip_0002.png``, … (four digits at least; longer videos simply count on).
The image format is the caller's pick (:class:`FrameFormat`): PNG, PNG with
light compression (faster, larger files), or JPEG (fastest, lossy), and so
is the sampling (:class:`FrameOptions`): every frame, or only some frames
per second — an evenly spaced "every Nth frame" pick.
Each kept frame is written exactly once (no duplicates or drops to fake a
constant frame rate), with any rotation flag applied, so the images look
like the video does in a player. The source file is only read.

Pure functions + one blocking :func:`extract_frames`: no Qt here — the worker
in ``workers/frames_worker.py`` runs this off the GUI thread.
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .rotator import RotateCancelledError, RotateError, SourceInfo, run_ffmpeg

log = logging.getLogger(__name__)

#: Appended to the video's name (extension dropped) for its frames folder.
FOLDER_SUFFIX = "_frames"


class FrameFormat(enum.Enum):
    PNG = "png"
    PNG_FAST = "png-fast"
    JPEG = "jpeg"

    @property
    def label(self) -> str:
        return {"png": "PNG", "png-fast": "PNG (fast)", "jpeg": "JPEG"}[self.value]

    @property
    def suffix(self) -> str:
        return ".jpg" if self is FrameFormat.JPEG else ".png"

    @property
    def encoder_args(self) -> list[str]:
        if self is FrameFormat.PNG_FAST:
            # zlib's lightest setting: still lossless, ~1.5x quicker.
            return ["-compression_level", "1"]
        if self is FrameFormat.JPEG:
            return ["-q:v", "2"]  # the encoder's best practical quality
        return []


#: Frames kept per second, offered per frame-rate family. The last entry of
#: each row is the family's own rate: every frame.
SAMPLE_PRESETS: dict[int, tuple[int, ...]] = {
    24: (2, 4, 6, 12, 24),
    30: (3, 5, 10, 15, 30),
}


def fps_family(fps: float | None) -> int:
    """24 or 30: the preset row a video at *fps* belongs to.

    Whichever has the nearer multiple: 23.976, 25, 48, 50 -> 24;
    29.97, 59.94, 60, 120 -> 30. An unknown rate counts as 30.
    """
    if not fps or fps <= 0:
        return 30

    def distance(base: int) -> float:
        return abs(fps - max(1, round(fps / base)) * base)

    return 24 if distance(24) < distance(30) else 30


@dataclass(slots=True, frozen=True)
class FrameOptions:
    """What one extraction run writes: image format + frames per second.

    The defaults are what the options dialog starts on: every frame as PNG
    is rarely what anyone wants (slow, and gigabytes per minute of video).
    """

    fmt: FrameFormat = FrameFormat.JPEG
    rate_24: int = 6   # frames kept per second of a 24 fps-family video
    rate_30: int = 10  # ... and of a 30 fps-family video

    def step(self, fps: float | None) -> int:
        """N of "keep every Nth frame" for a video at *fps* (1 = all)."""
        family = fps_family(fps)
        rate = self.rate_24 if family == 24 else self.rate_30
        if rate >= family:
            return 1  # the full preset means every frame, whatever the fps
        return max(1, round((fps or family) / rate))


class FramesCancelledError(RotateCancelledError):
    """The user cancelled while this file's frames were being extracted."""


class FramesError(RotateError):
    """ffmpeg could not extract the frames (message is user-presentable)."""


# -- paths -------------------------------------------------------------------


def frames_dir(src: Path, root: Path | None = None) -> Path:
    """``<name>_frames`` next to *src* (or inside *root*), never an existing
    path: ``<name>_frames (n)`` when that is taken, so an earlier extraction
    (or ``clip.mp4`` beside ``clip.mkv``) is not mixed into this one."""
    parent = root if root is not None else src.parent
    candidate = parent / f"{src.stem}{FOLDER_SUFFIX}"
    n = 1
    while candidate.exists():
        candidate = parent / f"{src.stem}{FOLDER_SUFFIX} ({n})"
        n += 1
    return candidate


def frame_pattern(
    src: Path, folder: Path, fmt: FrameFormat = FrameFormat.PNG
) -> str:
    """ffmpeg image-sequence pattern: ``<folder>/<name>_%04d.png`` (or .jpg)."""
    # A literal % anywhere in the path would be read as a pattern directive.
    prefix = str(folder / f"{src.stem}_").replace("%", "%%")
    return f"{prefix}%04d{fmt.suffix}"


# -- command building ----------------------------------------------------------


def build_command(
    ffmpeg: str,
    src: Path,
    folder: Path,
    fmt: FrameFormat = FrameFormat.PNG,
    step: int = 1,
) -> list[str]:
    """Write every *step*-th frame of *src* (1 = all of them) into *folder*."""
    # Counting frames, not resampling by time: the kept frames are evenly
    # spaced and real, and the images are still numbered 1, 2, 3, …
    sampling = ["-vf", f"select=not(mod(n\\,{step}))"] if step > 1 else []
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
        "-i", str(src),
        # First real video stream only (cover art is not a frame).
        "-map", "0:V:0", "-an", "-sn",
        *sampling,
        # The image muxer defaults to a constant frame rate, duplicating or
        # dropping frames of variable-rate sources: write each one once.
        "-fps_mode", "passthrough",
        *fmt.encoder_args,
        "-start_number", "1",
        "-progress", "pipe:1", "-nostats",
        frame_pattern(src, folder, fmt),
    ]


# -- running -----------------------------------------------------------------


def extract_frames(
    src: Path,
    folder: Path,
    info: SourceInfo | None,
    fmt: FrameFormat = FrameFormat.PNG,
    step: int = 1,
    *,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    ffmpeg: str | None = None,
) -> int:
    """Write every *step*-th frame of *src* into the new *folder*; returns
    the number of images written.

    Raises :class:`FramesCancelledError` when *cancel_event* is set and
    :class:`FramesError` on failure. A cancellation removes *folder*; a
    failure keeps the frames written before it (ffmpeg saves each image as
    it goes, and an error near the end must not cost the whole sequence) and
    says so in the error message. Only an empty *folder* is removed.
    """
    ffmpeg = ffmpeg or config.ffmpeg_path()
    if not ffmpeg:
        raise FramesError("ffmpeg not found")
    try:
        # Not exist_ok: the folder is removed again on failure, so it must
        # be ours alone.
        folder.mkdir(parents=True)
    except OSError as exc:
        raise FramesError(
            f"cannot create {folder} ({exc.strerror or exc})"
        ) from exc
    cmd = build_command(ffmpeg, src, folder, fmt, step)
    try:
        error = run_ffmpeg(
            cmd, info.duration_s if info else 0.0, on_progress, cancel_event
        )
    except RotateCancelledError:
        shutil.rmtree(folder, ignore_errors=True)
        raise FramesCancelledError from None
    count = _count_frames(folder, fmt.suffix)
    if error is None and count > 0:
        return count
    error = error or "ffmpeg produced no frames"
    log.warning("extracting frames of %s failed: %s", src, error)
    if count == 0:
        shutil.rmtree(folder, ignore_errors=True)
        raise FramesError(error)
    raise FramesError(
        f"stopped after {count} frame{'' if count == 1 else 's'}, kept in "
        f"{folder} ({error})"
    )


def _count_frames(folder: Path, suffix: str) -> int:
    try:
        with os.scandir(folder) as entries:
            return sum(1 for e in entries if e.name.lower().endswith(suffix))
    except OSError:
        return 0
