"""ffmpeg-based aspect-ratio change (horizontal stretch / squash).

The height stays as it is and the width is rescaled so the picture fills the
target ratio exactly — nothing is cropped or padded. Square pixels are
written (``setsar=1``) so every player shows the new shape.

Everything else follows :mod:`.rotator`: the video is re-encoded with the
source codec (NVENC + CUDA decoding first, CPU fallback) at the source
bitrate — raised in proportion when stretching, so the extra pixels do not
thin out the bits per pixel; never lowered when squashing. Audio,
subtitles, chapters, and metadata are stream-copied, and the result is
installed with :func:`rotator.install_rotated` (overwrite, or keep
the original in ``.vpbackup/``).

Pure functions + one blocking :func:`reshape_file`: no Qt here — the worker
in ``workers/aspect_worker.py`` runs this off the GUI thread.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .rotator import SourceInfo, reencode_file


@dataclass(frozen=True, slots=True)
class AspectRatio:
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.width}:{self.height}"

    def width_for(self, height: int) -> int:
        """Output width at *height*: what :attr:`video_filter` produces."""
        return int(height * self.width / self.height / 2) * 2

    @property
    def video_filter(self) -> str:
        # Keep the (display-oriented) height; even width for 4:2:0 chroma.
        return (
            f"scale=w=trunc(ih*{self.width}/{self.height}/2)*2:h=ih,setsar=1"
        )


#: Context-menu presets, widest first.
PRESETS: tuple[AspectRatio, ...] = (
    AspectRatio(21, 9),
    AspectRatio(16, 9),
    AspectRatio(16, 10),
    AspectRatio(3, 2),
    AspectRatio(4, 3),
    AspectRatio(1, 1),
    AspectRatio(3, 4),
    AspectRatio(9, 16),
)


def already_at(info: SourceInfo, ratio: AspectRatio) -> bool:
    """True when *info*'s picture already has *ratio* (re-encoding would only
    cost quality). Unknown geometry is never "already there"."""
    w, h = info.display_width, info.display_height
    if not w or not h:
        return False
    # The filter rounds to an even width: within a couple of pixels is a match.
    return abs(ratio.width_for(h) - w) <= 2


def target_bitrate(info: SourceInfo, ratio: AspectRatio) -> int | None:
    """Source video bitrate scaled by the width gain (never below the source).

    A stretch adds interpolated pixels only; scaling the bitrate with the
    pixel count slightly overshoots what they need, erring toward quality.
    """
    bitrate, w, h = info.video_bitrate, info.display_width, info.display_height
    if not bitrate or not w or not h:
        return bitrate
    return round(bitrate * max(1.0, ratio.width_for(h) / w))


def temp_output_path(src: Path) -> Path:
    """Scratch file next to *src*: dot-prefixed so no scan ever lists it."""
    return src.with_name(f".{src.stem}.reshaping{src.suffix}")


def reshape_file(
    src: Path,
    out: Path,
    ratio: AspectRatio,
    info: SourceInfo | None,
    *,
    use_cuda: bool,
    on_progress: Callable[[float], None] | None = None,
    on_encoder: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    ffmpeg: str | None = None,
) -> str:
    """Encode *src* stretched to *ratio* into *out*; returns the encoder used.

    Raises :class:`rotator.RotateCancelledError` on cancel and
    :class:`rotator.RotateError` when every encoder failed; *out* never
    survives either.
    """
    if info is not None:
        info = dataclasses.replace(info, video_bitrate=target_bitrate(info, ratio))
    return reencode_file(
        src, out, ratio.video_filter, info,
        use_cuda=use_cuda, on_progress=on_progress, on_encoder=on_encoder,
        cancel_event=cancel_event, ffmpeg=ffmpeg, action="changing ratio of",
    )
