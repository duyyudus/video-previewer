"""External media tools stay hidden when launched by the GUI app."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from video_previewer.media import metadata, thumbnailer
from video_previewer.media.process_flags import WINDOWLESS_CREATION_FLAGS
from video_previewer.media.thumbnailer import ExtractOutcome


def test_windowless_creation_flags_match_platform() -> None:
    if sys.platform == "win32":
        assert WINDOWLESS_CREATION_FLAGS == subprocess.CREATE_NO_WINDOW
    else:
        assert WINDOWLESS_CREATION_FLAGS == 0


def test_probe_launches_without_a_console(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"streams": [], "format": {"duration": "1"}}',
            stderr="",
        )

    monkeypatch.setattr(metadata.subprocess, "run", fake_run)

    assert metadata.probe_video(Path("movie.mp4"), ffprobe="ffprobe") is not None
    assert seen["creationflags"] == WINDOWLESS_CREATION_FLAGS


def test_thumbnail_launches_without_a_console(tmp_path, monkeypatch) -> None:
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"video")
    out = tmp_path / "thumb.jpg"
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        Path(cmd[-1]).write_bytes(b"jpeg")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(thumbnailer.subprocess, "run", fake_run)

    assert (
        thumbnailer.extract_thumbnail(video, out, ffmpeg="ffmpeg")
        is ExtractOutcome.OK
    )
    assert seen["creationflags"] == WINDOWLESS_CREATION_FLAGS
