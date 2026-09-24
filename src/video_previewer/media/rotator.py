"""ffmpeg-based 90° video rotation.

A real rotation re-encodes the video stream (``transpose``): a metadata-only
display-matrix flag is ignored by too many players and containers. To keep
the result close to the original in quality *and* size, the video is
re-encoded with the source codec at the source video bitrate, while audio,
subtitles, chapters, and metadata are stream-copied untouched.

NVIDIA NVENC (+ CUDA decoding) is used when the local ffmpeg and GPU support
it; any GPU failure falls back to the CPU encoder for that file.

Pure functions + one blocking :func:`rotate_file`: no Qt here — the worker in
``workers/rotate_worker.py`` runs this off the GUI thread.
"""

from __future__ import annotations

import enum
import functools
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import config
from .process_flags import WINDOWLESS_CREATION_FLAGS

log = logging.getLogger(__name__)

#: Folder (next to the video) that receives originals kept as backups.
BACKUP_DIR_NAME = ".vpbackup"


class RotateDirection(enum.Enum):
    CLOCKWISE = "clockwise"
    COUNTER_CLOCKWISE = "counter-clockwise"

    @property
    def transpose(self) -> str:
        # transpose=1: 90° clockwise; transpose=2: 90° counter-clockwise.
        return "transpose=1" if self is RotateDirection.CLOCKWISE else "transpose=2"

    @property
    def label(self) -> str:
        return "Clockwise" if self is RotateDirection.CLOCKWISE else "Counter-clockwise"


class RotateCancelledError(Exception):
    """The user cancelled while this file was being encoded."""


class RotateError(Exception):
    """ffmpeg could not rotate the file (message is user-presentable)."""


@dataclass(slots=True)
class SourceInfo:
    duration_s: float
    vcodec: str | None
    video_bitrate: int | None  # bits/s, best estimate; None when unknown
    # Picture size as players show it (rotation + pixel aspect applied);
    # None when ffprobe did not report it.
    display_width: int | None = None
    display_height: int | None = None


# (CPU candidates in preference order, NVENC encoder or None) per source codec.
_ENCODERS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "h264": (("libx264",), "h264_nvenc"),
    "hevc": (("libx265",), "hevc_nvenc"),
    "av1": (("libsvtav1", "libaom-av1"), "av1_nvenc"),
    "vp9": (("libvpx-vp9",), None),
    "vp8": (("libvpx",), None),
}
MP4_LIKE = frozenset({".mp4", ".m4v", ".mov"})


# -- capability detection ---------------------------------------------------


@functools.lru_cache(maxsize=4)
def available_encoders(ffmpeg: str) -> frozenset[str]:
    """Encoder names compiled into *ffmpeg* (empty on any failure)."""
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=config.PROBE_TIMEOUT,
            creationflags=WINDOWLESS_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return frozenset()
    names = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        # " V....D libx264   description" — flags column, then the name.
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


@functools.lru_cache(maxsize=8)
def nvenc_usable(ffmpeg: str, encoder: str) -> bool:
    """True when *encoder* actually runs here (GPU + driver present).

    Being compiled into ffmpeg says nothing about the machine: encode one
    tiny frame to find out. Cached for the process lifetime.
    """
    if encoder not in available_encoders(ffmpeg):
        return False
    cmd = [
        ffmpeg, "-hide_banner", "-v", "error",
        "-f", "lavfi", "-i", "color=black:s=256x256:d=0.1",
        "-frames:v", "1", "-c:v", encoder, "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=config.PROBE_TIMEOUT,
            creationflags=WINDOWLESS_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    usable = proc.returncode == 0
    log.info("%s %s", encoder, "available" if usable else "not usable on this system")
    return usable


# -- probing -----------------------------------------------------------------


def probe_source(path: Path, ffprobe: str | None = None) -> SourceInfo | None:
    """Duration, codec, and video bitrate of *path* (None on failure)."""
    ffprobe = ffprobe or config.ffprobe_path()
    if not ffprobe:
        return None
    cmd = [
        ffprobe, "-v", "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,bit_rate,width,height,sample_aspect_ratio,"
        "disposition:stream_tags:stream_side_data=rotation",
        "-show_entries", "format=duration,bit_rate,size",
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
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    videos = [
        s for s in streams
        if s.get("codec_type") == "video"
        and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not videos:
        return None
    video = videos[0]
    duration = _to_float(fmt.get("duration")) or 0.0
    width, height = display_size(video)
    return SourceInfo(
        duration_s=duration,
        vcodec=video.get("codec_name"),
        video_bitrate=_video_bitrate(video, streams, fmt, duration),
        display_width=width,
        display_height=height,
    )


def display_size(video: dict) -> tuple[int | None, int | None]:
    """(width, height) of *video* as shown: what ffmpeg's filters see.

    Non-square pixels widen or narrow the picture; a 90°/270° rotation flag
    (display matrix, or the legacy ``rotate`` tag) swaps the axes because
    ffmpeg's autorotate applies it before any filter.
    """
    width, height = to_int(video.get("width")), to_int(video.get("height"))
    if not width or not height:
        return None, None
    num, _, den = str(video.get("sample_aspect_ratio") or "").partition(":")
    sar_num, sar_den = to_int(num), to_int(den)
    if sar_num and sar_den and sar_num != sar_den:
        width = round(width * sar_num / sar_den)
    rotation = next(
        (
            r for d in video.get("side_data_list") or []
            if (r := to_int(d.get("rotation"))) is not None
        ),
        to_int((video.get("tags") or {}).get("rotate")) or 0,
    )
    if rotation % 180 == 90:  # Python: -90 % 180 == 90 too
        width, height = height, width
    return width, height


def _video_bitrate(
    video: dict, streams: list[dict], fmt: dict, duration: float
) -> int | None:
    """Best estimate of the video stream's bitrate in bits/s.

    MP4/MOV report it per stream; Matroska usually only carries a ``BPS``
    tag; otherwise it is the container's total minus the other streams.
    """
    rate = stream_bitrate(video)
    if rate:
        return rate
    total = to_int(fmt.get("bit_rate"))
    size = to_int(fmt.get("size"))
    if not total and size and duration > 0:
        total = int(size * 8 / duration)
    if not total:
        return None
    others = sum(stream_bitrate(s) or 0 for s in streams if s is not video)
    # Never trust a subtraction that eats most of the file (bad tags).
    return max(total - others, total // 4)


def stream_bitrate(stream: dict) -> int | None:
    rate = to_int(stream.get("bit_rate"))
    if rate:
        return rate
    for key, value in (stream.get("tags") or {}).items():
        if key.upper().startswith("BPS"):
            rate = to_int(value)
            if rate:
                return rate
    return None


# -- command building ----------------------------------------------------------


def choose_encoders(
    vcodec: str | None, suffix: str, ffmpeg: str, use_cuda: bool
) -> list[str]:
    """Encoders to try, fastest first (NVENC, then the CPU encoder).

    The source codec is kept whenever this ffmpeg can encode it (same
    compression efficiency -> same size at the same quality); anything else
    becomes H.264, or VP9 for WebM (the only codecs that container takes).
    """
    available = available_encoders(ffmpeg)
    cpu_candidates, nvenc = _ENCODERS.get(vcodec or "", ((), None))
    cpu = next((e for e in cpu_candidates if e in available), None)
    if cpu is None:
        # Unsupported/unknown source codec (mpeg4, prores, ...): transcode to
        # a codec every supported container accepts.
        if suffix.lower() == ".webm":
            cpu, nvenc = "libvpx-vp9", None
        else:
            cpu, nvenc = "libx264", "h264_nvenc"
    chain: list[str] = []
    if use_cuda and nvenc and nvenc_usable(ffmpeg, nvenc):
        chain.append(nvenc)
    chain.append(cpu)
    return chain


def encoder_args(encoder: str, bitrate: int | None) -> list[str]:
    """Rate-control arguments that aim at the source bitrate.

    Average-bitrate targeting keeps the file size close to the original; the
    peak cap leaves room for complex scenes. Without a known bitrate, fall
    back to a high-quality constant-quality mode.
    """
    args = ["-c:v", encoder]
    if encoder.endswith("_nvenc"):
        args += [
            "-preset", "p6", "-tune", "hq", "-rc", "vbr",
            "-multipass", "qres", "-spatial-aq", "1",
        ]
        if bitrate:
            # NVENC's VBR overshoots a loose cap (~10%); a tight one keeps
            # the size within a few percent of the original.
            args += _abr(bitrate, peak=1.1)
        else:
            args += ["-cq", "21", "-b:v", "0"]
    elif encoder in ("libx264", "libx265"):
        args += ["-preset", "medium"]
        if encoder == "libx265":
            args += ["-x265-params", "log-level=error"]
        args += _abr(bitrate) if bitrate else ["-crf", "18" if encoder == "libx264" else "20"]
    elif encoder in ("libvpx-vp9", "libvpx"):
        args += ["-deadline", "good", "-cpu-used", "2", "-row-mt", "1"]
        args += _abr(bitrate) if bitrate else ["-crf", "30", "-b:v", "0"]
    elif encoder == "libsvtav1":
        args += ["-preset", "8"]
        args += ["-b:v", str(bitrate)] if bitrate else ["-crf", "30"]
    elif encoder == "libaom-av1":
        args += ["-cpu-used", "6", "-row-mt", "1"]
        args += ["-b:v", str(bitrate)] if bitrate else ["-crf", "30", "-b:v", "0"]
    elif bitrate:
        args += ["-b:v", str(bitrate)]
    return args


def _abr(bitrate: int, peak: float = 1.5) -> list[str]:
    return [
        "-b:v", str(bitrate),
        "-maxrate", str(int(bitrate * peak)),
        "-bufsize", str(bitrate * 2),
    ]


def build_command(
    ffmpeg: str,
    src: Path,
    out: Path,
    video_filter: str,
    encoder: str,
    bitrate: int | None,
) -> list[str]:
    """Re-encode the video through *video_filter*; other streams are copied."""
    suffix = src.suffix.lower()
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y"]
    if encoder.endswith("_nvenc"):
        # Decode on the GPU too. Frames are downloaded to system memory for
        # the (cheap) filter; ffmpeg decodes in software on its own when
        # CUDA cannot handle the source codec.
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-i", str(src)]
    # First real video stream + every audio/subtitle stream, as-is. ffmpeg's
    # autorotate applies any existing rotation flag first, so the filter
    # works on the picture as the user sees it.
    cmd += ["-map", "0:V:0", "-map", "0:a?", "-map", "0:s?"]
    if suffix == ".mkv":
        cmd += ["-map", "0:t?"]  # embedded fonts etc.
    cmd += ["-vf", video_filter]
    cmd += encoder_args(encoder, bitrate)
    if encoder in ("libx265", "hevc_nvenc") and suffix in MP4_LIKE:
        cmd += ["-tag:v", "hvc1"]  # QuickTime / Apple players need hvc1
    cmd += [
        "-c:a", "copy", "-c:s", "copy",
        "-map_metadata", "0", "-map_chapters", "0",
        "-max_muxing_queue_size", "4096",
    ]
    if suffix in MP4_LIKE:
        cmd += ["-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(out)]
    return cmd


# -- running -----------------------------------------------------------------


def temp_output_path(src: Path) -> Path:
    """Scratch file next to *src*: dot-prefixed so no scan ever lists it."""
    return src.with_name(f".{src.stem}.rotating{src.suffix}")


def rotate_file(
    src: Path,
    out: Path,
    direction: RotateDirection,
    info: SourceInfo | None,
    *,
    use_cuda: bool,
    on_progress: Callable[[float], None] | None = None,
    on_encoder: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    ffmpeg: str | None = None,
) -> str:
    """Encode a rotated copy of *src* into *out*; returns the encoder used.

    See :func:`reencode_file` for the encoder chain and error contract.
    """
    return reencode_file(
        src, out, direction.transpose, info,
        use_cuda=use_cuda, on_progress=on_progress, on_encoder=on_encoder,
        cancel_event=cancel_event, ffmpeg=ffmpeg, action="rotating",
    )


def reencode_file(
    src: Path,
    out: Path,
    video_filter: str,
    info: SourceInfo | None,
    *,
    use_cuda: bool,
    on_progress: Callable[[float], None] | None = None,
    on_encoder: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    ffmpeg: str | None = None,
    action: str = "re-encoding",
) -> str:
    """Encode *src* through *video_filter* into *out*; returns the encoder used.

    Tries each encoder from :func:`choose_encoders` in turn (NVENC first).
    Raises :class:`RotateCancelledError` when *cancel_event* is set and
    :class:`RotateError` when every encoder failed. *out* never survives a
    failure or cancellation.
    """
    ffmpeg = ffmpeg or config.ffmpeg_path()
    if not ffmpeg:
        raise RotateError("ffmpeg not found")
    vcodec = info.vcodec if info else None
    bitrate = info.video_bitrate if info else None
    duration = info.duration_s if info else 0.0
    last_error = "unknown error"
    for encoder in choose_encoders(vcodec, src.suffix, ffmpeg, use_cuda):
        if on_encoder is not None:
            on_encoder(encoder)
        cmd = build_command(ffmpeg, src, out, video_filter, encoder, bitrate)
        try:
            error = run_ffmpeg(cmd, duration, on_progress, cancel_event)
        except RotateCancelledError:
            unlink_quietly(out)
            raise
        if error is None and out.exists() and out.stat().st_size > 0:
            return encoder
        unlink_quietly(out)
        last_error = error or "ffmpeg produced no output"
        log.warning("%s %s with %s failed: %s", action, src, encoder, last_error)
    raise RotateError(last_error)


def run_ffmpeg(
    cmd: list[str],
    duration: float,
    on_progress: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
) -> str | None:
    """Run one ffmpeg encode; None on success, else its error text."""
    # stderr goes to a file, not a pipe: a corrupt source can log an error
    # per frame, and a full pipe nobody drains would stall ffmpeg forever.
    with tempfile.TemporaryFile() as errfile:
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=errfile,
                stdin=subprocess.DEVNULL, text=True,
                creationflags=WINDOWLESS_CREATION_FLAGS,
            )
        except OSError as exc:
            return f"cannot run ffmpeg ({exc})"
        watcher = _CancelWatcher(proc, cancel_event)
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key == "out_time_us" and on_progress and duration > 0:
                    us = to_int(value)
                    if us is not None:
                        on_progress(min(1.0, max(0.0, us / 1e6 / duration)))
            proc.wait()
        finally:
            watcher.stop()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if cancel_event is not None and cancel_event.is_set():
            raise RotateCancelledError
        if proc.returncode == 0:
            return None
        errfile.seek(0)
        text = errfile.read().decode("utf-8", "replace").strip()
        tail = text.splitlines()[-3:] if text else []
        return " / ".join(tail) or f"ffmpeg exited with code {proc.returncode}"


class _CancelWatcher:
    """Kill *proc* as soon as *event* is set (the reader may be blocked)."""

    def __init__(self, proc: subprocess.Popen, event: threading.Event | None) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if event is None:
            return

        def watch() -> None:
            while not self._stop.is_set():
                if event.wait(0.1):
                    if proc.poll() is None:
                        proc.kill()
                    return

        self._thread = threading.Thread(target=watch, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()


# -- installing the result -------------------------------------------------------


def install_rotated(src: Path, rotated: Path, overwrite: bool) -> Path | None:
    """Put *rotated* in place of *src*; returns the backup path, if any.

    The original's timestamps carry over so a date-sorted grid keeps the
    video where it was. Without *overwrite* the original moves into
    ``.vpbackup/`` next to it (never clobbering an earlier backup). On failure
    the original is left (or put back) where it was and *rotated* removed.
    """
    try:
        st = src.stat()
        os.utime(rotated, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError as exc:
        log.debug("could not carry timestamps over to %s: %s", rotated, exc)
    if overwrite:
        try:
            retry_file_op(os.replace, rotated, src)
        except OSError:
            unlink_quietly(rotated)
            raise
        return None
    backup = unique_backup_path(src)
    try:
        backup.parent.mkdir(exist_ok=True)
        retry_file_op(os.replace, src, backup)
    except OSError:
        unlink_quietly(rotated)
        raise
    try:
        retry_file_op(os.replace, rotated, src)
    except OSError:
        # Never leave the user with neither file where they expect it.
        try:
            os.replace(backup, src)
        except OSError:
            log.error("original of %s left at %s", src, backup)
        unlink_quietly(rotated)
        raise
    return backup


def unique_backup_path(src: Path) -> Path:
    folder = src.parent / BACKUP_DIR_NAME
    candidate = folder / src.name
    n = 1
    while candidate.exists():
        candidate = folder / f"{src.stem} ({n}){src.suffix}"
        n += 1
    return candidate


def retry_file_op(fn: Callable[..., object], *args: object, attempts: int = 10) -> None:
    """Retry a file operation briefly: Windows AV/indexers hold files a moment."""
    for i in range(attempts):
        try:
            fn(*args)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.2)


def unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def to_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
