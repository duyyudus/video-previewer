"""ffmpeg-based conversion of non-MP4 videos to MP4.

Quality first: whenever MP4 can carry the source streams as they are, the
file is only *remuxed* (stream copy — lossless and fast). Only streams MP4
cannot hold are converted:

* video in H.264 / HEVC / AV1 / MPEG-4 Part 2 is copied; anything else is
  re-encoded — to HEVC for VP9 (similar efficiency), to H.264 otherwise —
  at the source bitrate for delivery codecs (so size and quality stay close
  to the original), or at a visually-lossless constant quality for
  intra/lossless sources (ProRes, MJPEG, FFV1, …) whose bitrate is huge;
* audio in AAC / MP3 / AC-3 / E-AC-3 / ALAC / FLAC / Opus is copied, the
  rest becomes AAC at (at least) the source bitrate;
* text subtitles become ``mov_text``; image subtitles (PGS, VobSub) cannot
  go into MP4 and are dropped. Chapters and metadata are kept.

Re-encoding tries NVIDIA NVENC (+ CUDA decoding) first when available and
falls back to the CPU; a failed remux also falls back to re-encoding.

Pure functions + one blocking :func:`convert_file`: no Qt here — the worker
in ``workers/convert_worker.py`` runs this off the GUI thread.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .. import config
from .process_flags import WINDOWLESS_CREATION_FLAGS
from .rotator import (
    MP4_LIKE,
    RotateCancelledError,
    RotateError,
    SourceInfo,
    choose_encoders,
    encoder_args,
    probe_source,
    retry_file_op,
    run_ffmpeg,
    stream_bitrate,
    to_int,
    unique_backup_path,
    unlink_quietly,
)

log = logging.getLogger(__name__)

#: Pseudo-encoder name for a lossless remux of the video stream.
STREAM_COPY = "copy"

_COPY_VIDEO = frozenset({"h264", "hevc", "av1", "mpeg4"})
# Inter-frame delivery codecs: H.264/HEVC at the same bitrate is at least as
# good. Everything else (intra-frame or lossless) gets constant quality.
_DELIVERY_VIDEO = frozenset({
    "vp9", "vp8", "mpeg1video", "mpeg2video", "wmv1", "wmv2", "wmv3", "vc1",
    "msmpeg4v1", "msmpeg4v2", "msmpeg4v3", "theora", "flv1", "h263",
    "rv10", "rv20", "rv30", "rv40", "vp6", "vp6f",
})
_COPY_AUDIO = frozenset({"aac", "mp3", "ac3", "eac3", "alac", "flac", "opus"})
_TEXT_SUBTITLES = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"})
_AAC_MIN, _AAC_MAX = 128_000, 512_000


class ConvertCancelledError(RotateCancelledError):
    """The user cancelled while this file was being converted."""


class ConvertError(RotateError):
    """ffmpeg could not convert the file (message is user-presentable)."""


class OriginalKeptError(Exception):
    """The MP4 is in place, but the original could not be deleted."""


def is_mp4_like(path: Path) -> bool:
    """True when *path* already is an MP4-family file (nothing to convert)."""
    return path.suffix.lower() in MP4_LIKE


@dataclass(slots=True)
class AudioStream:
    index: int
    codec: str | None
    channels: int
    bitrate: int | None


@dataclass(slots=True)
class ConvertPlan:
    """What to do with each stream of one source file."""

    info: SourceInfo
    video_index: int
    audio: list[AudioStream] = field(default_factory=list)
    subtitles: list[int] = field(default_factory=list)  # text streams only

    @property
    def video_copyable(self) -> bool:
        return (self.info.vcodec or "") in _COPY_VIDEO

    @property
    def video_target(self) -> str:
        """Codec a re-encode produces (HEVC for VP9, H.264 otherwise)."""
        return "hevc" if self.info.vcodec == "vp9" else "h264"

    @property
    def target_bitrate(self) -> int | None:
        """Source bitrate for delivery codecs; None = constant quality."""
        if (self.info.vcodec or "") in _DELIVERY_VIDEO:
            return self.info.video_bitrate
        return None


# -- probing -----------------------------------------------------------------


def probe_plan(path: Path, ffprobe: str | None = None) -> ConvertPlan | None:
    """Stream layout of *path* as a :class:`ConvertPlan` (None on failure)."""
    ffprobe = ffprobe or config.ffprobe_path()
    info = probe_source(path, ffprobe)
    if not ffprobe or info is None:
        return None
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,channels,bit_rate,disposition:stream_tags",
        "-of", "json", str(path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=config.PROBE_TIMEOUT,
            creationflags=WINDOWLESS_CREATION_FLAGS,
        )
        data = json.loads(proc.stdout or "{}") if proc.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        data = None
    if not data:
        return None
    return plan_from_streams(info, data.get("streams") or [])


def plan_from_streams(info: SourceInfo, streams: list[dict]) -> ConvertPlan | None:
    video_index: int | None = None
    audio: list[AudioStream] = []
    subtitles: list[int] = []
    for s in streams:
        index = to_int(s.get("index"))
        if index is None:
            continue
        kind = s.get("codec_type")
        if kind == "video":
            if video_index is None and not (s.get("disposition") or {}).get("attached_pic"):
                video_index = index
        elif kind == "audio":
            audio.append(AudioStream(
                index, s.get("codec_name"), to_int(s.get("channels")) or 2,
                stream_bitrate(s),
            ))
        elif kind == "subtitle" and s.get("codec_name") in _TEXT_SUBTITLES:
            subtitles.append(index)
    if video_index is None:
        return None
    return ConvertPlan(info, video_index, audio, subtitles)


# -- command building ----------------------------------------------------------


def aac_bitrate(stream: AudioStream) -> int:
    """AAC bitrate that keeps up with the source (96 kb/s per channel floor)."""
    wanted = max(stream.bitrate or 0, 96_000 * max(1, stream.channels))
    return min(_AAC_MAX, max(_AAC_MIN, wanted))


def build_command(
    ffmpeg: str, src: Path, out: Path, plan: ConvertPlan, encoder: str
) -> list[str]:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y"]
    if encoder == STREAM_COPY:
        cmd += ["-fflags", "+genpts"]  # AVI & co. may lack presentation times
    elif encoder.endswith("_nvenc"):
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src), "-map", f"0:{plan.video_index}"]
    cmd += [arg for a in plan.audio for arg in ("-map", f"0:{a.index}")]
    cmd += [arg for s in plan.subtitles for arg in ("-map", f"0:{s}")]
    if encoder == STREAM_COPY:
        cmd += ["-c:v", "copy"]
        if plan.info.vcodec == "mpeg4":
            cmd += ["-bsf:v", "mpeg4_unpack_bframes"]  # DivX packed B-frames
        target = plan.info.vcodec
    else:
        cmd += encoder_args(encoder, plan.target_bitrate)
        # The encoder, not the plan: an ffmpeg without x265 falls back to H.264.
        target = "hevc" if encoder in ("libx265", "hevc_nvenc") else "h264"
        if target == "h264":
            cmd += ["-pix_fmt", "yuv420p"]  # the only H.264 profile players agree on
    if target == "hevc":
        cmd += ["-tag:v", "hvc1"]  # QuickTime / Apple players need hvc1
    for n, a in enumerate(plan.audio):
        if a.codec in _COPY_AUDIO:
            cmd += [f"-c:a:{n}", "copy"]
        else:
            cmd += [f"-c:a:{n}", "aac", f"-b:a:{n}", str(aac_bitrate(a))]
    if plan.subtitles:
        cmd += ["-c:s", "mov_text"]
    cmd += [
        "-map_metadata", "0", "-map_chapters", "0",
        "-max_muxing_queue_size", "4096",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-progress", "pipe:1", "-nostats", str(out),
    ]
    return cmd


def encoder_chain(plan: ConvertPlan, ffmpeg: str, use_cuda: bool) -> list[str]:
    """Attempts in order: remux (when possible), then NVENC, then the CPU."""
    chain = choose_encoders(plan.video_target, ".mp4", ffmpeg, use_cuda)
    return [STREAM_COPY, *chain] if plan.video_copyable else chain


# -- paths -------------------------------------------------------------------


def output_path(src: Path) -> Path:
    """``name.mp4`` next to *src*, or ``name (n).mp4`` if that is taken."""
    candidate = src.with_suffix(".mp4")
    n = 1
    while candidate.exists():
        candidate = src.with_name(f"{src.stem} ({n}).mp4")
        n += 1
    return candidate


def temp_output_path(src: Path) -> Path:
    """Scratch file next to *src*: dot-prefixed so no scan ever lists it."""
    return src.with_name(f".{src.stem}.converting.mp4")


# -- running -----------------------------------------------------------------


def convert_file(
    src: Path,
    out: Path,
    plan: ConvertPlan,
    *,
    use_cuda: bool,
    on_progress: Callable[[float], None] | None = None,
    on_encoder: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    ffmpeg: str | None = None,
) -> str:
    """Write an MP4 version of *src* into *out*; returns the encoder used.

    Raises :class:`ConvertCancelledError` when *cancel_event* is set and
    :class:`ConvertError` when every attempt failed. *out* never survives a
    failure or cancellation.
    """
    ffmpeg = ffmpeg or config.ffmpeg_path()
    if not ffmpeg:
        raise ConvertError("ffmpeg not found")
    last_error = "unknown error"
    for encoder in encoder_chain(plan, ffmpeg, use_cuda):
        if on_encoder is not None:
            on_encoder(encoder)
        cmd = build_command(ffmpeg, src, out, plan, encoder)
        try:
            error = run_ffmpeg(cmd, plan.info.duration_s, on_progress, cancel_event)
        except RotateCancelledError:
            unlink_quietly(out)
            raise ConvertCancelledError from None
        if error is None and out.exists() and out.stat().st_size > 0:
            return encoder
        unlink_quietly(out)
        last_error = error or "ffmpeg produced no output"
        log.warning("converting %s with %s failed: %s", src, encoder, last_error)
    raise ConvertError(last_error)


# -- installing the result -------------------------------------------------------


def install_converted(src: Path, converted: Path, dest: Path, overwrite: bool) -> Path | None:
    """Move *converted* to *dest* and retire *src*; returns the backup path.

    The original's timestamps carry over so a date-sorted grid keeps the
    video where it was. With *overwrite* the original is deleted; otherwise
    it moves into ``.vpbackup/`` next to it. On failure nothing is lost: the
    original stays (or is put back) and *converted* is removed.
    """
    try:
        st = src.stat()
        os.utime(converted, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError as exc:
        log.debug("could not carry timestamps over to %s: %s", converted, exc)
    backup = None
    if not overwrite:
        backup = unique_backup_path(src)
        try:
            backup.parent.mkdir(exist_ok=True)
            retry_file_op(os.replace, src, backup)
        except OSError:
            unlink_quietly(converted)
            raise
    try:
        retry_file_op(os.replace, converted, dest)
    except OSError:
        if backup is not None:
            try:
                os.replace(backup, src)
            except OSError:
                log.error("original of %s left at %s", src, backup)
        unlink_quietly(converted)
        raise
    if overwrite:
        try:
            retry_file_op(os.remove, src)
        except OSError as exc:
            # Both files exist: nothing is lost, but the user should know.
            raise OriginalKeptError(exc.strerror or str(exc)) from exc
    return backup

