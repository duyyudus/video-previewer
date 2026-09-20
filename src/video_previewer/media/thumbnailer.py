"""ffmpeg-based thumbnail frame extraction."""

from __future__ import annotations

import enum
import logging
import os
import subprocess
from pathlib import Path

from .. import config
from .process_flags import WINDOWLESS_CREATION_FLAGS

log = logging.getLogger(__name__)


class ExtractOutcome(enum.Enum):
    """Result of a thumbnail extraction attempt.

    The distinction matters for the negative cache: only ``FAILED`` says
    something about *the file* (ffmpeg ran and refused it), so only ``FAILED``
    may mark a video as permanently unthumbnable. ``TRANSIENT`` covers
    everything else — binary gone, file temporarily offline, seek timeout,
    disk full — which must be retried on the next launch instead of poisoning
    the file for as long as its size/mtime stay the same.
    """

    OK = "ok"
    FAILED = "failed"
    TRANSIENT = "transient"


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
) -> ExtractOutcome:
    """Extract one JPEG frame from *video* into *out*."""
    ffmpeg = ffmpeg or config.ffmpeg_path()
    if not ffmpeg:
        log.warning("ffmpeg not found; cannot extract thumbnail for %s", video)
        return ExtractOutcome.TRANSIENT
    if not video.exists():
        # Deleted between the scan and this job, or sitting on a network share
        # / removable drive that is offline right now: says nothing about the
        # file, so it must stay retryable.
        return ExtractOutcome.TRANSIENT
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Unwritable cache dir: report it once per file as a transient failure
        # (the caller drops further work when the cache dir is unusable).
        log.warning("cannot create thumbnail dir %s (%s)", out.parent, exc)
        return ExtractOutcome.TRANSIENT

    primary_t = thumbnail_time(duration_ms)
    outcome = _extract(ffmpeg, video, out, primary_t, width)
    if outcome is not ExtractOutcome.FAILED:
        # Success, or a timeout/spawn/write problem: the t=0 fallback would
        # just double the worst case (another full extract timeout).
        return outcome
    # Fallback: a few videos refuse a seek (very short, weird timestamps).
    if primary_t != 0.0:
        fallback = _extract(ffmpeg, video, out, 0.0, width)
        if fallback is not ExtractOutcome.FAILED:
            # Either the frame at t=0 worked, or the second attempt hit a
            # transient problem — in neither case may the file be blamed.
            return fallback
    log.warning("thumbnail extraction failed for %s", video)
    return ExtractOutcome.FAILED


def _extract(ffmpeg: str, video: Path, out: Path, t: float, width: int) -> ExtractOutcome:
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
            cmd,
            capture_output=True,
            text=True,
            timeout=config.THUMB_EXTRACT_TIMEOUT,
            creationflags=WINDOWLESS_CREATION_FLAGS,
        )
    except subprocess.TimeoutExpired:
        # A slow seek (huge/4K file, network-mounted folder) is not a broken
        # file: retry next launch rather than giving up on it forever.
        _unlink(tmp)
        return ExtractOutcome.TRANSIENT
    except OSError as exc:
        # ffmpeg vanished (uninstalled mid-session) or the process could not
        # be started at all.
        log.warning("cannot run ffmpeg (%s)", exc)
        _unlink(tmp)
        return ExtractOutcome.TRANSIENT
    if proc.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
        try:
            os.replace(tmp, out)
            return ExtractOutcome.OK
        except OSError as exc:
            # Disk full or an AV/indexer lock on the cache file: the video is
            # fine, so this must not be recorded as a permanent failure.
            log.warning("cannot store thumbnail %s (%s)", out, exc)
            _unlink(tmp)
            return ExtractOutcome.TRANSIENT
    # ffmpeg ran to completion and refused the file: permanently broken until
    # the file's identity (size/mtime/path) changes.
    _unlink(tmp)
    return ExtractOutcome.FAILED


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
