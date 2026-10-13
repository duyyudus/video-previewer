"""Extract frames: ffmpeg core, grid menu, settings tab, window flow."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QPushButton,
)

from conftest import make_video, pump
from video_previewer import config
from video_previewer.media import frames, rotator
from video_previewer.media.frames import FrameFormat, FrameOptions
from video_previewer.ui import rotate_dialog
from video_previewer.ui.main_window import MainWindow

# -- pure helpers ------------------------------------------------------------------


def test_frames_dir_next_to_video_or_in_root_never_clobbers(tmp_path):
    src = tmp_path / "vids" / "my clip.mkv"
    assert frames.frames_dir(src) == tmp_path / "vids" / "my clip_frames"
    root = tmp_path / "out"
    assert frames.frames_dir(src, root) == root / "my clip_frames"
    (root / "my clip_frames").mkdir(parents=True)
    assert frames.frames_dir(src, root) == root / "my clip_frames (1)"


def test_build_command_names_frames_after_the_video(tmp_path):
    src, folder = tmp_path / "clip.mp4", tmp_path / "clip_frames"
    cmd = frames.build_command("ffmpeg", src, folder)
    assert cmd[-1] == str(folder / "clip_%04d.png")
    joined = " ".join(cmd)
    assert "-map 0:V:0" in joined and "-fps_mode passthrough" in joined
    assert "-start_number 1" in joined
    # A % in the file name must not be read as a pattern directive.
    assert frames.frame_pattern(tmp_path / "50%.mp4", folder).endswith("50%%_%04d.png")
    # Formats: light PNG compression, or JPEG with its own extension.
    assert "-compression_level" not in cmd
    fast = frames.build_command("ffmpeg", src, folder, FrameFormat.PNG_FAST)
    assert "-compression_level 1" in " ".join(fast) and fast[-1].endswith(".png")
    jpeg = frames.build_command("ffmpeg", src, folder, FrameFormat.JPEG)
    assert "-q:v 2" in " ".join(jpeg) and jpeg[-1].endswith("clip_%04d.jpg")


def test_sampling_step_from_typed_rate():
    assert FrameOptions() == FrameOptions(FrameFormat.JPEG, 1.0)
    every = FrameOptions(rate=None)
    assert [every.step(f) for f in (24, 30, 60, None)] == [1, 1, 1, 1]
    # N frames per second = every (fps / N)th frame, whatever the video's rate.
    assert [FrameOptions(rate=r).step(24) for r in (0.5, 1, 2, 6, 12, 24)] == [
        48, 24, 12, 4, 2, 1,
    ]
    assert [FrameOptions(rate=r).step(30) for r in (0.5, 1, 3, 10, 15, 30)] == [
        60, 30, 10, 3, 2, 1,
    ]
    assert FrameOptions(rate=1).step(59.94) == 60
    # Rounded to whole frames; never fewer than every frame.
    assert FrameOptions(rate=5).step(24) == 5 and FrameOptions(rate=120).step(24) == 1
    assert FrameOptions(rate=10).step(None) == 3  # unknown rate: assume 30 fps


def test_build_command_samples_every_nth_frame(tmp_path):
    src, folder = tmp_path / "clip.mp4", tmp_path / "clip_frames"
    assert "-vf" not in frames.build_command("ffmpeg", src, folder)
    cmd = frames.build_command("ffmpeg", src, folder, FrameFormat.JPEG, 4)
    assert cmd[cmd.index("-vf") + 1] == "select=not(mod(n\\,4))"
    assert "-fps_mode passthrough" in " ".join(cmd)  # no duplicated frames


def test_probe_reports_frame_rate():
    assert rotator.frame_rate({"avg_frame_rate": "30000/1001"}) == pytest.approx(29.97, abs=0.01)
    assert rotator.frame_rate({"avg_frame_rate": "0/0", "r_frame_rate": "24/1"}) == 24
    assert rotator.frame_rate({}) is None


# -- real ffmpeg ---------------------------------------------------------------------


def test_extract_frames_writes_every_frame(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=1.0)  # 24 fps
    folder = frames.frames_dir(src)
    progress: list[float] = []
    count = frames.extract_frames(
        src, folder, rotator.probe_source(src), on_progress=progress.append
    )
    names = sorted(p.name for p in folder.iterdir())
    assert count == len(names) == 24
    assert names[0] == "clip_0001.png" and names[-1] == "clip_0024.png"
    assert (folder / "clip_0001.png").read_bytes().startswith(b"\x89PNG")
    assert progress and progress[-1] > 0.5


def test_extract_frames_sampled(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=2.0)  # 24 fps: 48 frames
    info = rotator.probe_source(src)
    assert info.fps == pytest.approx(24)
    step = FrameOptions(rate=6).step(info.fps)
    folder = frames.frames_dir(src)
    assert frames.extract_frames(src, folder, info, FrameFormat.JPEG, step) == 12
    names = sorted(p.name for p in folder.iterdir())
    assert names == [f"clip_{n:04d}.jpg" for n in range(1, 13)]  # no gaps


def test_extract_one_frame_per_second(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=3.0)  # 24 fps: 72 frames
    info = rotator.probe_source(src)
    folder = frames.frames_dir(src)
    step = FrameOptions(rate=1).step(info.fps)
    assert frames.extract_frames(src, folder, info, FrameFormat.JPEG, step) == 3
    # Half a frame per second: one image every 2 seconds (frames 0 and 48).
    folder = frames.frames_dir(src)
    step = FrameOptions(rate=0.5).step(info.fps)
    assert frames.extract_frames(src, folder, info, FrameFormat.JPEG, step) == 2


def test_extract_frames_formats(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=1.0)
    sizes = {}
    for fmt, magic in (
        (FrameFormat.PNG, b"\x89PNG"),
        (FrameFormat.PNG_FAST, b"\x89PNG"),
        (FrameFormat.JPEG, b"\xff\xd8"),
    ):
        folder = frames.frames_dir(src)  # a fresh "(n)" folder each time
        assert frames.extract_frames(src, folder, None, fmt) == 24
        first = folder / f"clip_0001{fmt.suffix}"
        assert first.read_bytes().startswith(magic)
        sizes[fmt] = sum(p.stat().st_size for p in folder.iterdir())
    assert sizes[FrameFormat.PNG] < sizes[FrameFormat.PNG_FAST]


def test_extract_frames_handles_percent_in_name(tmp_path):
    src = make_video(tmp_path / "50%.mp4", seconds=0.5)
    folder = frames.frames_dir(src)
    assert frames.extract_frames(src, folder, None) == 12
    assert (folder / "50%_0001.png").is_file()


def test_extract_frames_cancel_and_failure_leave_no_folder(tmp_path):
    src = make_video(tmp_path / "clip.mp4", seconds=2.0)
    folder = frames.frames_dir(src)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(frames.FramesCancelledError):
        frames.extract_frames(src, folder, None, cancel_event=cancel)
    assert not folder.exists()

    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")
    folder = frames.frames_dir(broken)
    with pytest.raises(frames.FramesError):
        frames.extract_frames(broken, folder, None)
    assert not folder.exists()


def test_extract_frames_error_keeps_frames_already_written(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    folder = frames.frames_dir(src)

    def failing_run(cmd, duration, on_progress, cancel_event):
        for n in (1, 2, 3):
            (folder / f"clip_{n:04d}.png").write_bytes(b"png")
        return "No space left on device"

    monkeypatch.setattr(frames, "run_ffmpeg", failing_run)
    with pytest.raises(frames.FramesError, match="stopped after 3 frames") as exc:
        frames.extract_frames(src, folder, None, ffmpeg="ffmpeg")
    assert "No space left on device" in str(exc.value)
    assert len(list(folder.iterdir())) == 3


# -- grid context menu ------------------------------------------------------------------


def _open(win: MainWindow, qapp, folder: Path, count: int) -> None:
    win.show()
    win._open_folder(folder)
    assert pump(qapp, lambda: win.model.count() == count, timeout=30)
    for _ in range(3):
        QApplication.processEvents()


def _center(win: MainWindow, row: int) -> QPoint:
    return win.grid.visualRect(win.model.index(row)).center()


def test_context_menu_has_extract_frames(qapp, cache_dir, tmp_path):
    folder = tmp_path / "vids"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x" * 64)
    win = MainWindow()
    try:
        _open(win, qapp, folder, 1)
        grid = win.grid
        emitted: list[bool] = []
        grid.extract_frames_requested.disconnect()
        grid.extract_frames_requested.connect(lambda: emitted.append(True))
        menu = grid.context_menu_at(_center(win, 0))
        extract = next(a for a in menu.actions() if a.text() == "Extract frames")
        extract.trigger()
        assert emitted == [True]
    finally:
        win.close()


# -- settings tab ------------------------------------------------------------------------


def test_settings_frames_tab_sets_and_resets_location(
    qapp, cache_dir, tmp_path, monkeypatch
):
    from video_previewer.ui import settings_dialog as settings_dialog_mod

    target = tmp_path / "frames out"
    target.mkdir()
    monkeypatch.setattr(
        settings_dialog_mod.QFileDialog, "getExistingDirectory",
        lambda *a, **k: str(target),
    )
    win = MainWindow()
    win.show()
    try:
        assert win._frames_root() is None  # default: next to the video
        win._show_settings()
        dialog = win._settings_dialog
        label = dialog.findChild(QLabel, "framesPathLabel")
        reset = dialog.findChild(QPushButton, "framesResetButton")
        assert "default" in label.text() and not reset.isEnabled()

        dialog.findChild(QPushButton, "framesChooseButton").click()
        assert win._frames_root() == target
        assert label.text() == str(target) and reset.isEnabled()

        reset.click()
        assert win._frames_root() is None
        assert "default" in label.text() and not reset.isEnabled()
    finally:
        win.close()


# -- options dialog ----------------------------------------------------------------------


def test_options_dialog_defaults_and_reports_choices(qapp, tmp_path):
    dialog = rotate_dialog.FrameOptionsDialog(None, ["a.mp4", "b.mkv"], tmp_path)
    try:
        # Starts on JPEG at one frame per second.
        assert dialog.options() == FrameOptions(FrameFormat.JPEG, 1.0)
        rate = dialog.findChild(QDoubleSpinBox, "framesRate")
        every = dialog.findChild(QCheckBox, "framesAll")
        assert rate.isEnabled() and not every.isChecked()

        fmt = dialog.findChild(QComboBox, "framesFormat")
        fmt.setCurrentIndex(fmt.findData(FrameFormat.PNG))
        rate.setValue(0.5)
        assert dialog.options() == FrameOptions(FrameFormat.PNG, 0.5)
        rate.setValue(0)  # below the minimum: clamped, never zero
        assert dialog.options().rate == pytest.approx(0.01)

        every.setChecked(True)
        assert not rate.isEnabled()
        assert dialog.options() == FrameOptions(FrameFormat.PNG, None)
    finally:
        dialog.deleteLater()


# -- window flow -------------------------------------------------------------------------


def _extract(win: MainWindow, monkeypatch, options: FrameOptions | None) -> list:
    asked: list = []

    def ask(parent, names, root=None):
        asked.append((list(names), root))
        return options

    monkeypatch.setattr(rotate_dialog, "ask_frame_options", ask)
    win.grid.selectAll()
    win._extract_frames_selected()
    return asked


def test_extract_selection_next_to_videos(qapp, cache_dir, tmp_path, monkeypatch):
    folder = tmp_path / "vids"
    a = make_video(folder / "a.mp4", seconds=0.5)
    b = make_video(folder / "b.mkv", seconds=0.5)
    originals = (a.read_bytes(), b.read_bytes())
    win = MainWindow()
    try:
        _open(win, qapp, folder, 2)

        # Declining the options prompt does nothing.
        assert _extract(win, monkeypatch, None) == [(["a.mp4", "b.mkv"], None)]
        assert win._encode_job is None

        _extract(win, monkeypatch, FrameOptions(FrameFormat.PNG, None))
        assert win._encode_dialog is not None and win._encode_dialog.isModal()
        assert pump(qapp, lambda: win._encode_job is None, timeout=120)

        assert sorted(p.name for p in folder.iterdir()) == [
            "a.mp4", "a_frames", "b.mkv", "b_frames",
        ]
        assert len(list((folder / "a_frames").glob("a_????.png"))) == 12
        assert len(list((folder / "b_frames").glob("b_????.png"))) == 12
        assert (a.read_bytes(), b.read_bytes()) == originals
        assert win.model.count() == 2 and win._encode_dialog is None
    finally:
        win.close()


def test_extract_into_custom_location_and_cancel(
    qapp, cache_dir, tmp_path, monkeypatch
):
    from PySide6.QtCore import QSettings

    folder = tmp_path / "vids"
    make_video(folder / "a.mp4", seconds=4.0)
    root = tmp_path / "elsewhere" / "frames"  # created on demand
    QSettings().setValue(config.SETTING_FRAMES_DIR, str(root))
    win = MainWindow()
    try:
        _open(win, qapp, folder, 1)

        # Cancel right away: no partial folder is left behind.
        options = FrameOptions(FrameFormat.JPEG, rate=6)  # every 4th frame
        assert _extract(win, monkeypatch, options) == [(["a.mp4"], root)]
        dialog = win._encode_dialog
        dialog.close()  # closing the progress dialog cancels
        assert dialog.cancelling
        assert pump(qapp, lambda: win._encode_job is None, timeout=60)
        assert not (root / "a_frames").exists()

        _extract(win, monkeypatch, options)
        assert pump(qapp, lambda: win._encode_job is None, timeout=120)
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4"]
        assert len(list((root / "a_frames").iterdir())) == 24  # 96 frames / 4
        assert (root / "a_frames" / "a_0001.jpg").is_file()
    finally:
        win.close()
