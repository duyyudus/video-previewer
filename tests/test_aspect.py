"""Change ratio: filter + ffmpeg core, grid menu, window flow."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from conftest import HAS_FFMPEG, make_video, pump
from video_previewer import config
from video_previewer.media import aspect, rotator
from video_previewer.media.aspect import AspectRatio
from video_previewer.media.rotator import SourceInfo
from video_previewer.ui import rotate_dialog
from video_previewer.ui.main_window import MainWindow


def _geometry(path: Path) -> tuple[int, int, str]:
    out = subprocess.run(
        [
            config.ffprobe_path(), "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,sample_aspect_ratio",
            "-of", "csv=p=0", str(path),
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    w, h, sar = out.split(",")[:3]
    return int(w), int(h), sar


# -- pure helpers ------------------------------------------------------------------


def test_presets_and_filter():
    labels = [r.label for r in aspect.PRESETS]
    assert labels[:5] == ["21:9", "16:9", "16:10", "3:2", "4:3"]
    vf = AspectRatio(4, 3).video_filter
    assert vf.startswith("scale=w=trunc(ih*4/3/2)*2:h=ih")
    assert vf.endswith("setsar=1")


def _info(w: int | None, h: int | None, bitrate: int | None = 1_000_000) -> SourceInfo:
    return SourceInfo(10.0, "h264", bitrate, w, h)


def test_display_size_applies_sar_and_rotation():
    assert rotator.display_size({"width": 320, "height": 180}) == (320, 180)
    # Anamorphic: 720x576 with 64:45 pixels shows as 1024x576.
    anamorphic = {"width": 720, "height": 576, "sample_aspect_ratio": "64:45"}
    assert rotator.display_size(anamorphic) == (1024, 576)
    # 90° either way (display matrix or legacy tag) swaps the axes.
    for video in (
        {"width": 320, "height": 180, "side_data_list": [{"rotation": -90}]},
        {"width": 320, "height": 180, "tags": {"rotate": "270"}},
    ):
        assert rotator.display_size(video) == (180, 320)
    flipped = {"width": 320, "height": 180, "side_data_list": [{"rotation": 180}]}
    assert rotator.display_size(flipped) == (320, 180)
    assert rotator.display_size({"width": 0}) == (None, None)


def test_already_at_tolerates_even_rounding():
    assert aspect.already_at(_info(1920, 1080), AspectRatio(16, 9))
    assert aspect.already_at(_info(854, 480), AspectRatio(16, 9))  # 853.3 -> 852
    assert not aspect.already_at(_info(1440, 1080), AspectRatio(16, 9))
    assert not aspect.already_at(_info(None, None), AspectRatio(16, 9))


def test_target_bitrate_scales_up_only():
    # 4:3 -> 16:9 at 1080 lines: 1440 -> 1920 wide, 4/3 the pixels.
    assert aspect.target_bitrate(_info(1440, 1080, 3_000_000), AspectRatio(16, 9)) == 4_000_000
    # Squashing keeps the source bitrate.
    assert aspect.target_bitrate(_info(1920, 1080, 3_000_000), AspectRatio(4, 3)) == 3_000_000
    # Unknown geometry or bitrate: unchanged.
    assert aspect.target_bitrate(_info(None, None, 3_000_000), AspectRatio(16, 9)) == 3_000_000
    assert aspect.target_bitrate(_info(1440, 1080, None), AspectRatio(16, 9)) is None


def test_reshape_file_encodes_at_scaled_bitrate(tmp_path, monkeypatch):
    seen: list[SourceInfo | None] = []
    monkeypatch.setattr(
        aspect, "reencode_file", lambda src, out, vf, info, **kw: seen.append(info) or "x"
    )
    source = _info(1440, 1080, 3_000_000)
    aspect.reshape_file(
        tmp_path / "a.mp4", tmp_path / "o.mp4", AspectRatio(16, 9), source, use_cuda=False
    )
    assert seen[0].video_bitrate == 4_000_000
    assert source.video_bitrate == 3_000_000  # the caller's info is untouched


def test_build_command_uses_ratio_filter_and_cuda(tmp_path):
    cmd = rotator.build_command(
        "ffmpeg-test", tmp_path / "a.mp4", tmp_path / "o.mp4",
        AspectRatio(16, 9).video_filter, "h264_nvenc", 1000,
    )
    assert cmd[cmd.index("-hwaccel") + 1] == "cuda"
    assert cmd[cmd.index("-vf") + 1] == AspectRatio(16, 9).video_filter
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_temp_output_is_hidden_from_scans(tmp_path):
    tmp = aspect.temp_output_path(tmp_path / "clip.mkv")
    assert tmp.name.startswith(".") and tmp.suffix == ".mkv"


# -- ffmpeg pipeline -------------------------------------------------------------------


def test_probe_reports_display_geometry(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=1, width=320, height=180)
    info = rotator.probe_source(src)
    assert (info.display_width, info.display_height) == (320, 180)
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            config.ffmpeg_path(), "-v", "error", "-y", "-display_rotation", "90",
            "-i", str(src), "-c", "copy", str(rotated),
        ],
        check=True, capture_output=True,
    )
    info = rotator.probe_source(rotated)
    assert (info.display_width, info.display_height) == (180, 320)


def test_reshape_file_cpu_squashes_width_keeps_height(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=1.5, width=320, height=180)
    out = tmp_path / "out.mp4"
    encoder = aspect.reshape_file(
        src, out, AspectRatio(4, 3), rotator.probe_source(src), use_cuda=False
    )
    assert encoder == "libx264"
    w, h, sar = _geometry(out)
    assert (w, h) == (240, 180)
    assert sar in ("1:1", "N/A")


def test_reshape_file_gpu_when_available(tmp_path):
    if not HAS_FFMPEG or not rotator.nvenc_usable(config.ffmpeg_path(), "h264_nvenc"):
        pytest.skip("NVENC not available")
    src = make_video(tmp_path / "clip.mp4", seconds=1.5, width=240, height=180)
    out = tmp_path / "out.mp4"
    encoder = aspect.reshape_file(
        src, out, AspectRatio(16, 9), rotator.probe_source(src), use_cuda=True
    )
    assert encoder == "h264_nvenc"
    assert _geometry(out)[:2] == (320, 180)


# -- grid context menu ------------------------------------------------------------------


def _open(win: MainWindow, qapp, folder: Path, count: int) -> None:
    win.show()
    win._open_folder(folder)
    assert pump(qapp, lambda: win.model.count() == count, timeout=30)
    for _ in range(3):
        QApplication.processEvents()


def test_context_menu_orders_and_emits_ratio(qapp, cache_dir, tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    (folder / "a.mkv").write_bytes(b"x" * 64)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 1)
        grid = win.grid
        emitted: list[AspectRatio] = []
        grid.aspect_requested.disconnect()
        grid.aspect_requested.connect(emitted.append)

        center = grid.visualRect(win.model.index(0)).center()
        menu = grid.context_menu_at(center)
        top = [a.text() for a in menu.actions()]
        assert top == ["Rotate", "Change ratio", "Convert to MP4"]
        ratio_menu = menu.actions()[1].menu()
        assert [a.text() for a in ratio_menu.actions()] == [
            r.label for r in aspect.PRESETS
        ]
        next(a for a in ratio_menu.actions() if a.text() == "4:3").trigger()
        assert emitted == [AspectRatio(4, 3)]
    finally:
        win.close()


# -- window flow -------------------------------------------------------------------------


def test_change_ratio_keeps_backup_and_refreshes_row(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.mp4", seconds=1.5, width=320, height=180)
    original = a.read_bytes()
    monkeypatch.setattr(config, "ASPECT_USE_CUDA", False)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 1)
        old = win.model.item_at(0)

        # Declining the prompt does nothing.
        monkeypatch.setattr(
            rotate_dialog, "ask_aspect_overwrite", lambda parent, names, ratio: None
        )
        win.grid.selectAll()
        win._change_ratio_selected(AspectRatio(4, 3))
        assert win._encode_job is None

        monkeypatch.setattr(
            rotate_dialog, "ask_aspect_overwrite", lambda parent, names, ratio: False
        )
        win._change_ratio_selected(AspectRatio(4, 3))
        assert win._encode_dialog is not None and win._encode_dialog.isModal()
        assert pump(qapp, lambda: win._encode_job is None, timeout=120)

        assert _geometry(a)[:2] == (240, 180)
        assert (folder / ".vpbackup" / "a.mp4").read_bytes() == original
        assert sorted(p.name for p in folder.iterdir()) == [".vpbackup", "a.mp4"]
        new = win.model.item_at(win.model.row_for(a))
        assert new.vid != old.vid
        assert win._db.get_video(old.vid) is None
    finally:
        win.close()


def test_change_ratio_skips_videos_already_at_target(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.mp4", seconds=1, width=320, height=180)
    b = make_video(folder / "b.mp4", seconds=1, width=240, height=180)
    original = a.read_bytes()
    monkeypatch.setattr(config, "ASPECT_USE_CUDA", False)
    monkeypatch.setattr(
        rotate_dialog, "ask_aspect_overwrite", lambda parent, names, ratio: True
    )
    notes: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "information", lambda parent, title, text: notes.append(text)
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder, 2)
        win.grid.selectAll()
        win._change_ratio_selected(AspectRatio(16, 9))
        assert pump(qapp, lambda: win._encode_job is None, timeout=120)

        assert a.read_bytes() == original  # already 16:9: never re-encoded
        assert _geometry(b)[:2] == (320, 180)
        assert len(notes) == 1 and "already 16:9" in notes[0] and "a.mp4" in notes[0]
    finally:
        win.close()
