"""ffmpeg-based thumbnail frame extraction."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)


def thumbnail_time(duration_ms: int | None) -> float:
    """Seek position (seconds) for the representative frame.

    ~15% into the video when it is long enough (frame zero is often black or
    a title card); a safe fallback for short/unknown videos.
    """
    if duration_ms and duration_ms > 2000:
        t = duration_ms * config.THUMB_POSITION_RATIO / 1000.0
        # never seek past the last 100 ms of the file
        return max(0.0, min(t, duration_ms / 1000.0 - 0.1))
    if duration_ms and duration_ms > 0:
        return max(0.0, duration_ms / 2000.0)
    return config.THUMB_FALLBACK_SECONDS


def extract_thumbnail(
    video: Path,
    out: Path,
    duration_ms: int | None = None,
    width: int = config.THUMB_WIDTH,
    ffmpeg: str | None = None,
) -> bool:
    """Extract one JPEG frame from *video* into *out*. Returns success."""
    ffmpeg = ffmpeg or config.ffmpeg_path()
    if not ffmpeg:
        log.warning("ffmpeg not found; cannot extract thumbnail for %s", video)
        return False
    if not video.exists():
        return False
    out.parent.mkdir(parents=True, exist_ok=True)

    primary_t = thumbnail_time(duration_ms)
    if _extract(ffmpeg, video, out, primary_t, width):
        return True
    # Fallback: a few videos refuse a seek (very short, weird timestamps).
    if primary_t != 0.0 and _extract(ffmpeg, video, out, 0.0, width):
        return True
    log.warning("thumbnail extraction failed for %s", video)
    return False


def _extract(ffmpeg: str, video: Path, out: Path, t: float, width: int) -> bool:
    tmp = out.with_name(out.name + ".tmp")
    cmd = [
        ffmpeg,
        "-v", "error",
        "-y",
        "-ss", f"{t:.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-f", "image2",  # temp filename has a .tmp extension; force JPEG output
        str(tmp),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.THUMB_EXTRACT_TIMEOUT
        )
    except (OSError, subprocess.TimeoutExpired):
        _unlink(tmp)
        return False
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        try:
            os.replace(tmp, out)
            return True
        except OSError:
            pass
    _unlink(tmp)
    return False


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
