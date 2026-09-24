"""Video rotation: ffmpeg core, backup/overwrite install, grid menu, window flow."""

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
from video_previewer.media import rotator
from video_previewer.media.rotator import RotateDirection
from video_previewer.ui import rotate_dialog
from video_previewer.ui.main_window import MainWindow

FFMPEG = "ffmpeg-test"


def _dims(path: Path) -> tuple[int, int]:
    out = subprocess.run(
        [
            config.ffprobe_path(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


def _has_audio(path: Path) -> bool:
    out = subprocess.run(
        [
            config.ffprobe_path(), "-v", "error", "-select_streams", "a",
            "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return bool(out)


# -- pure helpers ------------------------------------------------------------------


def test_direction_maps_to_transpose():
    assert RotateDirection.CLOCKWISE.transpose == "transpose=1"
    assert RotateDirection.COUNTER_CLOCKWISE.transpose == "transpose=2"


def test_choose_encoders_prefers_nvenc_then_cpu(monkeypatch):
    monkeypatch.setattr(
        rotator, "available_encoders",
        lambda ff: frozenset({"libx264", "h264_nvenc", "libx265", "libvpx-vp9"}),
    )
    monkeypatch.setattr(rotator, "nvenc_usable", lambda ff, enc: True)
    assert rotator.choose_encoders("h264", ".mp4", FFMPEG, True) == [
        "h264_nvenc", "libx264",
    ]
    assert rotator.choose_encoders("h264", ".mp4", FFMPEG, False) == ["libx264"]
    # No NVENC for VP9: CPU only.
    assert rotator.choose_encoders("vp9", ".webm", FFMPEG, True) == ["libvpx-vp9"]
    # Unencodable source codec: H.264, or VP9 inside WebM.
    assert rotator.choose_encoders("mpeg4", ".avi", FFMPEG, True) == [
        "h264_nvenc", "libx264",
    ]
    assert rotator.choose_encoders("av1", ".webm", FFMPEG, True) == ["libvpx-vp9"]


def test_choose_encoders_skips_unusable_gpu(monkeypatch):
    monkeypatch.setattr(
        rotator, "available_encoders",
        lambda ff: frozenset({"libx265", "hevc_nvenc"}),
    )
    monkeypatch.setattr(rotator, "nvenc_usable", lambda ff, enc: False)
    assert rotator.choose_encoders("hevc", ".mkv", FFMPEG, True) == ["libx265"]


def test_encoder_args_target_source_bitrate():
    args = rotator.encoder_args("libx264", 2_000_000)
    assert args[args.index("-b:v") + 1] == "2000000"
    nv = rotator.encoder_args("h264_nvenc", 2_000_000)
    assert nv[nv.index("-maxrate") + 1] == "2200000"
    # Unknown bitrate: constant quality instead of a guessed bitrate.
    assert "-crf" in rotator.encoder_args("libx264", None)


def test_build_command_copies_audio_and_tags_hevc(tmp_path):
    src = tmp_path / "a.mp4"
    cmd = rotator.build_command(
        FFMPEG, src, tmp_path / "o.mp4", RotateDirection.CLOCKWISE, "hevc_nvenc", 1000
    )
    assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
    assert cmd[cmd.index("-vf") + 1] == "transpose=1"
    assert cmd[cmd.index("-c:a") + 1] == "copy"
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
    cpu = rotator.build_command(
        FFMPEG, tmp_path / "a.mkv", tmp_path / "o.mkv",
        RotateDirection.COUNTER_CLOCKWISE, "libx264", 1000,
    )
    assert "-hwaccel" not in cpu
    assert "0:t?" in cpu


def test_video_bitrate_from_matroska_tag_or_container():
    video = {"codec_type": "video", "tags": {"BPS-eng": "1500000"}}
    assert rotator._video_bitrate(video, [video], {}, 10.0) == 1_500_000
    video = {"codec_type": "video"}
    audio = {"codec_type": "audio", "bit_rate": "128000"}
    fmt = {"bit_rate": "1128000"}
    assert rotator._video_bitrate(video, [video, audio], fmt, 10.0) == 1_000_000


def test_temp_output_is_hidden_from_scans(tmp_path):
    tmp = rotator.temp_output_path(tmp_path / "clip.mp4")
    assert tmp.name.startswith(".") and tmp.suffix == ".mp4"


def test_install_overwrite_keeps_timestamps(tmp_path):
    src = tmp_path / "a.mp4"
    src.write_bytes(b"old")
    os.utime(src, (1_000_000, 1_000_000))
    new = tmp_path / ".a.rotating.mp4"
    new.write_bytes(b"rotated")
    assert rotator.install_rotated(src, new, overwrite=True) is None
    assert src.read_bytes() == b"rotated"
    assert not new.exists()
    assert int(src.stat().st_mtime) == 1_000_000
    assert not (tmp_path / ".vpbackup").exists()


def test_install_backup_never_clobbers_earlier_backup(tmp_path):
    src = tmp_path / "a.mp4"
    (tmp_path / ".vpbackup").mkdir()
    (tmp_path / ".vpbackup" / "a.mp4").write_bytes(b"first")
    src.write_bytes(b"second")
    new = tmp_path / ".a.rotating.mp4"
    new.write_bytes(b"rotated")
    backup = rotator.install_rotated(src, new, overwrite=False)
    assert backup == tmp_path / ".vpbackup" / "a (1).mp4"
    assert backup.read_bytes() == b"second"
    assert (tmp_path / ".vpbackup" / "a.mp4").read_bytes() == b"first"
    assert src.read_bytes() == b"rotated"


# -- ffmpeg pipeline -------------------------------------------------------------------


def test_rotate_file_cpu_rotates_and_keeps_audio(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=2, width=320, height=180)
    out = tmp_path / "out.mp4"
    info = rotator.probe_source(src)
    assert info is not None and info.vcodec == "h264" and info.video_bitrate
    seen: list[float] = []
    encoder = rotator.rotate_file(
        src, out, RotateDirection.CLOCKWISE, info,
        use_cuda=False, on_progress=seen.append,
    )
    assert encoder == "libx264"
    assert _dims(out) == (180, 320)
    assert _has_audio(out)
    assert seen and all(0.0 <= f <= 1.0 for f in seen)


def test_rotate_file_gpu_when_available(tmp_path):
    if not HAS_FFMPEG or not rotator.nvenc_usable(config.ffmpeg_path(), "h264_nvenc"):
        pytest.skip("NVENC not available")
    src = make_video(tmp_path / "clip.mp4", seconds=2, width=320, height=180)
    out = tmp_path / "out.mp4"
    encoder = rotator.rotate_file(
        src, out, RotateDirection.COUNTER_CLOCKWISE, rotator.probe_source(src),
        use_cuda=True,
    )
    assert encoder == "h264_nvenc"
    assert _dims(out) == (180, 320)


def test_rotate_file_cancel_leaves_no_output(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=2)
    out = tmp_path / "out.mp4"
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(rotator.RotateCancelledError):
        rotator.rotate_file(
            src, out, RotateDirection.CLOCKWISE, rotator.probe_source(src),
            use_cuda=False, cancel_event=cancel,
        )
    assert not out.exists()


def test_rotate_file_reports_failure(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg/ffprobe not available")
    src = tmp_path / "broken.mp4"
    src.write_bytes(b"not a video")
    with pytest.raises(rotator.RotateError):
        rotator.rotate_file(
            src, tmp_path / "o.mp4", RotateDirection.CLOCKWISE, None, use_cuda=False
        )
    assert not (tmp_path / "o.mp4").exists()


# -- grid context menu ------------------------------------------------------------------


def _open(win: MainWindow, qapp, folder: Path, count: int) -> None:
    win.show()
    win._open_folder(folder)
    assert pump(qapp, lambda: win.model.count() == count, timeout=30)
    for _ in range(3):
        QApplication.processEvents()


def _center(win: MainWindow, row: int) -> QPoint:
    return win.grid.visualRect(win.model.index(row)).center()


def test_context_menu_selects_clicked_tile_and_emits_rotate(qapp, cache_dir, tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    for name in ("a.mp4", "b.mp4"):
        (folder / name).write_bytes(b"x" * 64)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 2)
        grid = win.grid
        emitted: list[RotateDirection] = []
        grid.rotate_requested.disconnect()
        grid.rotate_requested.connect(emitted.append)

        assert grid.context_menu_at(QPoint(-50, -50)) is None  # empty space
        menu = grid.context_menu_at(_center(win, 1))
        assert [r.row() for r in grid.selectionModel().selectedRows()] == [1]
        rotate = next(a for a in menu.actions() if a.text() == "Rotate").menu()
        labels = [a.text() for a in rotate.actions()]
        assert labels == ["Clockwise", "Counter-clockwise"]
        rotate.actions()[1].trigger()
        assert emitted == [RotateDirection.COUNTER_CLOCKWISE]

        # Right-clicking inside a multi-selection keeps it.
        grid.selectAll()
        grid.context_menu_at(_center(win, 0))
        assert len(grid.selectionModel().selectedRows()) == 2
    finally:
        win.close()


# -- window flow -------------------------------------------------------------------------


def _rotate(win: MainWindow, qapp, monkeypatch, overwrite: bool | None) -> None:
    monkeypatch.setattr(
        rotate_dialog, "ask_overwrite", lambda parent, names, direction: overwrite
    )
    win.grid.selectAll()
    win._rotate_selected(RotateDirection.CLOCKWISE)


def test_rotate_selection_keeps_backups(qapp, cache_dir, tmp_path, monkeypatch):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.mp4", seconds=1.5)
    b = make_video(folder / "b.mp4", seconds=1.5)
    originals = {p.name: p.read_bytes() for p in (a, b)}
    monkeypatch.setattr(config, "ROTATE_USE_CUDA", False)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 2)
        old_items = [win.model.item_at(r) for r in range(2)]
        _rotate(win, qapp, monkeypatch, overwrite=False)
        assert win._rotate_dialog is not None and win._rotate_dialog.isModal()
        assert pump(qapp, lambda: win._rotate_job is None, timeout=120)

        for p in (a, b):
            assert _dims(p) == (180, 320)
            assert (folder / ".vpbackup" / p.name).read_bytes() == originals[p.name]
        # Only the videos themselves: temp files are gone, backups are hidden.
        assert sorted(p.name for p in folder.iterdir()) == [".vpbackup", "a.mp4", "b.mp4"]
        # Rows carry the new file identity; the old cache rows are gone.
        for old in old_items:
            row = win.model.row_for(old.path)
            new = win.model.item_at(row)
            assert new.size == old.path.stat().st_size
            assert new.vid != old.vid
            assert win._db.get_video(old.vid) is None
        assert win._rotate_dialog is None
    finally:
        win.close()


def test_rotate_overwrite_and_cancel(qapp, cache_dir, tmp_path, monkeypatch):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.mp4", seconds=1.5)
    monkeypatch.setattr(config, "ROTATE_USE_CUDA", False)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 1)

        # Declining the prompt does nothing.
        _rotate(win, qapp, monkeypatch, overwrite=None)
        assert win._rotate_job is None

        # Cancel right away: the original is untouched, no temp left behind.
        original = a.read_bytes()
        _rotate(win, qapp, monkeypatch, overwrite=True)
        dialog = win._rotate_dialog
        dialog.close()  # closing the progress dialog cancels
        assert dialog.cancelling and dialog.isVisible()  # stays up until done
        assert pump(qapp, lambda: win._rotate_job is None, timeout=60)
        assert a.read_bytes() == original
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4"]

        # Overwrite: rotated in place, no backup folder.
        _rotate(win, qapp, monkeypatch, overwrite=True)
        assert pump(qapp, lambda: win._rotate_job is None, timeout=120)
        assert _dims(a) == (180, 320)
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4"]
    finally:
        win.close()
