"""Convert to MP4: ffmpeg core, install, grid menu, window flow."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from conftest import HAS_FFMPEG, make_video, pump
from video_previewer import config
from video_previewer.media import converter, rotator
from video_previewer.media.rotator import SourceInfo
from video_previewer.ui import rotate_dialog
from video_previewer.ui.main_window import MainWindow

FFMPEG = "ffmpeg-test"


def _codecs(path: Path) -> list[tuple[str, str]]:
    """(codec_type, codec_name) of every stream, in order."""
    out = subprocess.run(
        [
            config.ffprobe_path(), "-v", "error",
            "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0",
            str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    return [tuple(line.split(",")[::-1]) for line in out]  # type: ignore[misc]


def _plan(vcodec: str, bitrate: int | None = 2_000_000, **kw) -> converter.ConvertPlan:
    return converter.ConvertPlan(SourceInfo(10.0, vcodec, bitrate), 0, **kw)


# -- pure helpers ------------------------------------------------------------------


def test_is_mp4_like():
    for name in ("a.mp4", "b.MP4", "c.m4v", "d.mov"):
        assert converter.is_mp4_like(Path(name))
    for name in ("a.mkv", "b.avi", "c.webm"):
        assert not converter.is_mp4_like(Path(name))


def test_plan_from_streams_keeps_mp4_compatible_streams():
    streams = [
        {"index": 0, "codec_type": "video", "codec_name": "mjpeg",
         "disposition": {"attached_pic": 1}},
        {"index": 1, "codec_type": "video", "codec_name": "h264"},
        {"index": 2, "codec_type": "audio", "codec_name": "vorbis", "channels": 6},
        {"index": 3, "codec_type": "subtitle", "codec_name": "subrip"},
        {"index": 4, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        {"index": 5, "codec_type": "attachment", "codec_name": "ttf"},
    ]
    plan = converter.plan_from_streams(SourceInfo(5.0, "h264", None), streams)
    assert plan.video_index == 1  # cover art skipped
    assert [(a.index, a.codec, a.channels) for a in plan.audio] == [(2, "vorbis", 6)]
    assert plan.subtitles == [3]  # image subtitles cannot go into MP4
    assert converter.plan_from_streams(SourceInfo(5.0, None, None), streams[2:]) is None


def test_encoder_chain_remuxes_first_then_gpu_then_cpu(monkeypatch):
    monkeypatch.setattr(
        rotator, "available_encoders",
        lambda ff: frozenset({"libx264", "h264_nvenc", "libx265", "hevc_nvenc"}),
    )
    monkeypatch.setattr(rotator, "nvenc_usable", lambda ff, enc: True)
    assert converter.encoder_chain(_plan("h264"), FFMPEG, True) == [
        "copy", "h264_nvenc", "libx264",
    ]
    assert converter.encoder_chain(_plan("hevc"), FFMPEG, False) == ["copy", "libx264"]
    assert converter.encoder_chain(_plan("vp9"), FFMPEG, True) == [
        "hevc_nvenc", "libx265",
    ]
    assert converter.encoder_chain(_plan("wmv2"), FFMPEG, False) == ["libx264"]


def test_build_command_remux(tmp_path):
    plan = _plan(
        "hevc",
        audio=[
            converter.AudioStream(1, "aac", 2, 128_000),
            converter.AudioStream(2, "dts", 6, 1_500_000),
            converter.AudioStream(3, "pcm_s16le", 2, None),
        ],
        subtitles=[4],
    )
    cmd = converter.build_command(
        "ffmpeg", tmp_path / "a.mkv", tmp_path / "o.mp4", plan, "copy"
    )
    joined = " ".join(cmd)
    assert "-c:v copy" in joined and "-tag:v hvc1" in joined
    assert "-map 0:0 -map 0:1 -map 0:2 -map 0:3 -map 0:4" in joined
    assert "-c:a:0 copy" in joined
    assert "-c:a:1 aac -b:a:1 512000" in joined  # capped
    assert "-c:a:2 aac -b:a:2 192000" in joined  # 96k per channel
    assert "-c:s mov_text" in joined
    assert "-movflags +faststart" in joined and cmd[-1].endswith("o.mp4")
    assert "-hwaccel" not in cmd


def test_build_command_reencode_rate_control(tmp_path):
    src, out = tmp_path / "a.avi", tmp_path / "o.mp4"
    # Delivery codec: aim at the source bitrate, GPU decode for NVENC.
    cmd = converter.build_command("ffmpeg", src, out, _plan("wmv2"), "h264_nvenc")
    joined = " ".join(cmd)
    assert "-hwaccel cuda" in joined and "-b:v 2000000" in joined
    assert "-pix_fmt yuv420p" in joined
    # Intra/lossless codec: constant quality instead of its huge bitrate.
    cmd = converter.build_command(
        "ffmpeg", src, out, _plan("prores", 150_000_000), "libx264"
    )
    assert "-crf" in cmd and "150000000" not in cmd
    # H.264 fallback for a VP9 source never gets the HEVC tag.
    cmd = converter.build_command("ffmpeg", src, out, _plan("vp9"), "libx264")
    assert "hvc1" not in cmd


def test_output_path_never_clobbers(tmp_path):
    src = tmp_path / "clip.mkv"
    assert converter.output_path(src) == tmp_path / "clip.mp4"
    (tmp_path / "clip.mp4").write_bytes(b"x")
    assert converter.output_path(src) == tmp_path / "clip (1).mp4"
    assert converter.temp_output_path(src).name.startswith(".")


def test_install_backup_and_overwrite(tmp_path):
    src = tmp_path / "a.mkv"
    src.write_bytes(b"orig")
    os.utime(src, (1_000_000, 1_000_000))
    tmp = tmp_path / ".a.converting.mp4"
    tmp.write_bytes(b"new")
    dest = tmp_path / "a.mp4"
    backup = converter.install_converted(src, tmp, dest, overwrite=False)
    assert backup == tmp_path / ".vpbackup" / "a.mkv"
    assert backup.read_bytes() == b"orig" and not src.exists()
    assert dest.read_bytes() == b"new" and dest.stat().st_mtime == 1_000_000

    src.write_bytes(b"orig2")
    tmp.write_bytes(b"new2")
    dest2 = tmp_path / "a (1).mp4"
    assert converter.install_converted(src, tmp, dest2, overwrite=True) is None
    assert not src.exists() and not tmp.exists() and dest2.read_bytes() == b"new2"


# -- real ffmpeg ---------------------------------------------------------------------


def test_convert_mkv_remuxes_losslessly(tmp_path):
    src = make_video(tmp_path / "a.mkv", seconds=1.5)
    plan = converter.probe_plan(src)
    out = tmp_path / "o.mp4"
    progress: list[float] = []
    encoder = converter.convert_file(
        src, out, plan, use_cuda=False, on_progress=progress.append
    )
    assert encoder == "copy"
    assert _codecs(out) == [("video", "h264"), ("audio", "aac")]
    assert progress and progress[-1] > 0.5


def test_convert_webm_reencodes_and_keeps_audio(tmp_path):
    src = make_video(
        tmp_path / "a.webm", seconds=1.0, vcodec="libvpx-vp9", acodec="libopus"
    )
    plan = converter.probe_plan(src)
    out = tmp_path / "o.mp4"
    encoder = converter.convert_file(src, out, plan, use_cuda=False)
    assert encoder in ("libx265", "libx264")
    assert _codecs(out) == [
        ("video", "hevc" if encoder == "libx265" else "h264"), ("audio", "opus"),
    ]


def test_convert_avi_transcodes_pcm_audio(tmp_path):
    src = make_video(tmp_path / "a.avi", seconds=1.0, vcodec="mpeg4", acodec="pcm_s16le")
    plan = converter.probe_plan(src)
    out = tmp_path / "o.mp4"
    assert converter.convert_file(src, out, plan, use_cuda=False) == "copy"
    assert _codecs(out) == [("video", "mpeg4"), ("audio", "aac")]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not available")
def test_convert_gpu_when_available(tmp_path):
    ffmpeg = config.ffmpeg_path()
    if not rotator.nvenc_usable(ffmpeg, "h264_nvenc"):
        pytest.skip("NVENC not usable here")
    src = make_video(tmp_path / "a.avi", seconds=1.0, vcodec="wmv2", acodec="libmp3lame")
    plan = converter.probe_plan(src)
    out = tmp_path / "o.mp4"
    assert converter.convert_file(src, out, plan, use_cuda=True) == "h264_nvenc"
    assert _codecs(out) == [("video", "h264"), ("audio", "mp3")]


def test_convert_cancel_leaves_no_output(tmp_path):
    src = make_video(tmp_path / "a.webm", seconds=2.0, vcodec="libvpx-vp9", acodec="libopus")
    plan = converter.probe_plan(src)
    out = tmp_path / "o.mp4"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(converter.ConvertCancelledError):
        converter.convert_file(src, out, plan, use_cuda=False, cancel_event=cancel)
    assert not out.exists()


# -- grid context menu ------------------------------------------------------------------


def _open(win: MainWindow, qapp, folder: Path, count: int) -> None:
    win.show()
    win._open_folder(folder)
    assert pump(qapp, lambda: win.model.count() == count, timeout=30)
    for _ in range(3):
        QApplication.processEvents()


def _center(win: MainWindow, row: int) -> QPoint:
    return win.grid.visualRect(win.model.index(row)).center()


def _row_of(win: MainWindow, name: str) -> int:
    return next(
        r for r in range(win.model.count()) if win.model.item_at(r).filename == name
    )


def test_context_menu_convert_disabled_for_mp4_only(qapp, cache_dir, tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    for name in ("a.mp4", "b.mkv"):
        (folder / name).write_bytes(b"x" * 64)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 2)
        grid = win.grid
        emitted: list[bool] = []
        grid.convert_requested.disconnect()
        grid.convert_requested.connect(lambda: emitted.append(True))

        menu = grid.context_menu_at(_center(win, _row_of(win, "a.mp4")))
        convert = next(a for a in menu.actions() if a.text() == "Convert to MP4")
        assert not convert.isEnabled()

        grid.selectAll()
        menu = grid.context_menu_at(_center(win, 0))
        convert = next(a for a in menu.actions() if a.text() == "Convert to MP4")
        assert convert.isEnabled()
        convert.trigger()
        assert emitted == [True]
    finally:
        win.close()


# -- window flow -------------------------------------------------------------------------


def _convert(win: MainWindow, monkeypatch, overwrite: bool | None) -> list:
    asked: list = []

    def ask(parent, names, skipped=0):
        asked.append((list(names), skipped))
        return overwrite

    monkeypatch.setattr(rotate_dialog, "ask_convert_overwrite", ask)
    win.grid.selectAll()
    win._convert_selected()
    return asked


def test_convert_selection_keeps_backups_and_skips_mp4(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.mkv", seconds=1.0)
    b = make_video(folder / "b.mp4", seconds=1.0)
    original = a.read_bytes()
    b_bytes = b.read_bytes()
    monkeypatch.setattr(config, "CONVERT_USE_CUDA", False)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 2)
        old = win.model.item_at(_row_of(win, "a.mkv"))
        asked = _convert(win, monkeypatch, overwrite=False)
        assert asked == [(["a.mkv"], 1)]
        assert win._encode_dialog is not None and win._encode_dialog.isModal()
        assert pump(qapp, lambda: win._encode_job is None, timeout=120)

        assert sorted(p.name for p in folder.iterdir()) == [".vpbackup", "a.mp4", "b.mp4"]
        assert (folder / ".vpbackup" / "a.mkv").read_bytes() == original
        assert b.read_bytes() == b_bytes  # already MP4: untouched
        # The tile now points at the new file; the old identity is gone.
        assert win.model.row_for(a) is None
        row = win.model.row_for(folder / "a.mp4")
        assert row is not None and win.model.count() == 2
        assert win.model.item_at(row).size == (folder / "a.mp4").stat().st_size
        assert win._db.get_video(old.vid) is None
        assert win._encode_dialog is None
    finally:
        win.close()


def test_convert_overwrite_and_cancel(qapp, cache_dir, tmp_path, monkeypatch):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.webm", seconds=2.0, vcodec="libvpx-vp9", acodec="libopus")
    monkeypatch.setattr(config, "CONVERT_USE_CUDA", False)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 1)

        # Declining the prompt does nothing.
        _convert(win, monkeypatch, overwrite=None)
        assert win._encode_job is None

        # Cancel right away: the original is untouched, no temp left behind.
        original = a.read_bytes()
        _convert(win, monkeypatch, overwrite=True)
        dialog = win._encode_dialog
        dialog.close()  # closing the progress dialog cancels
        assert dialog.cancelling and dialog.isVisible()
        assert pump(qapp, lambda: win._encode_job is None, timeout=60)
        assert a.read_bytes() == original
        assert sorted(p.name for p in folder.iterdir()) == ["a.webm"]

        # Overwrite: the original is deleted, no backup folder.
        _convert(win, monkeypatch, overwrite=True)
        assert pump(qapp, lambda: win._encode_job is None, timeout=120)
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4"]
        assert win.model.count() == 1
        assert win.model.item_at(0).filename == "a.mp4"
    finally:
        win.close()
