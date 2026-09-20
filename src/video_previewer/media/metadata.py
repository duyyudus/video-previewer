"""ffprobe-based metadata extraction."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .process_flags import WINDOWLESS_CREATION_FLAGS

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ProbeResult:
    duration_ms: int
    width: int | None
    height: int | None
    vcodec: str | None


def probe_video(path: Path, ffprobe: str | None = None) -> ProbeResult | None:
    """Probe a video file. Returns None on any failure (caller keeps placeholder)."""
    ffprobe = ffprobe or config.ffprobe_path()
    if not ffprobe:
        return None
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.PROBE_TIMEOUT,
            creationflags=WINDOWLESS_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("ffprobe failed for %s", path)
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    streams = data.get("streams") or []
    stream = streams[0] if streams else {}
    try:
        duration_ms = int(round(float(data.get("format", {}).get("duration", 0)) * 1000))
    except (TypeError, ValueError):
        duration_ms = 0
    width = _to_int(stream.get("width"))
    height = _to_int(stream.get("height"))
    vcodec = stream.get("codec_name")
    return ProbeResult(duration_ms, width, height, vcodec)


def _to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
