"""Delete / Shift+Delete actions for selected video tiles."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication

from conftest import pump
from video_previewer.ui.main_window import MainWindow
from video_previewer.workers.scanner import ScanResult


def _dummy_folder(tmp_path, names=("a.mp4", "b.mp4")) -> Path:
    folder = tmp_path / "vids"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"x" * 64)
    return folder


def _row_center(win: MainWindow, row: int) -> QPointF:
    for _ in range(3):
        QApplication.processEvents()
    rect = win.grid.visualRect(win.model.index(row))
    assert rect.isValid()
    return QPointF(rect.center())


def _click(
    win: MainWindow,
    row: int,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    pos = _row_center(win, row)
    for event_type in (
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
    ):
        QApplication.sendEvent(
            win.grid.viewport(),
            QMouseEvent(
                event_type,
                pos,
                pos,
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                modifiers,
            ),
        )


def _delete_key(win: MainWindow, shift: bool = False) -> None:
    modifiers = (
        Qt.KeyboardModifier.ShiftModifier
        if shift
        else Qt.KeyboardModifier.NoModifier
    )
    QApplication.sendEvent(
        win.grid,
        QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, modifiers),
    )


def _open(win: MainWindow, qapp, folder: Path, count: int = 2) -> None:
    win.show()
    win._open_folder(folder)
    assert pump(qapp, lambda: win.model.count() == count, timeout=30)


def test_delete_moves_selection_to_trash_and_cleans_caches(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path)
    moved: list[Path] = []

    def fake_trash(path: Path) -> bool:
        moved.append(path)
        path.unlink()
        return True

    monkeypatch.setattr(
        "video_previewer.ui.main_window.move_to_trash", fake_trash
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder)
        key = win._db.scan_key(folder, False)
        assert pump(qapp, lambda: win._db.load_scan(key) is not None, timeout=30)
        assert pump(qapp, lambda: win._thumbs.pending_count() == 0, timeout=30)

        items = [win.model.item_at(row) for row in range(2)]
        for item in items:
            thumb = cache_dir / f"{item.vid}.jpg"
            thumb.write_bytes(b"jpeg")
            win._cache.store(item, thumb, 1000, 320, 180, "h264")

        _click(win, 0)
        _click(win, 1, Qt.KeyboardModifier.ControlModifier)
        win.grid._on_pointer_move(_row_center(win, 0).toPoint())
        assert win.player.active_path is not None
        _delete_key(win)

        assert sorted(path.name for path in moved) == ["a.mp4", "b.mp4"]
        assert win.player.active_path is None
        assert win.model.count() == 0
        assert list(folder.iterdir()) == []
        assert win._db.load_scan(key) == []
        for item in items:
            assert win._db.get_video(item.vid) is None
            assert not (cache_dir / f"{item.vid}.jpg").exists()
    finally:
        win.close()


def test_shift_delete_cancel_keeps_files(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)
    confirmations: list[list[str]] = []
    trash_calls: list[Path] = []

    def cancel(parent, filenames) -> bool:
        confirmations.append(list(filenames))
        return False

    monkeypatch.setattr(
        "video_previewer.ui.main_window.delete_dialog.confirm_permanent_delete",
        cancel,
    )
    monkeypatch.setattr(
        "video_previewer.ui.main_window.move_to_trash",
        lambda path: trash_calls.append(path) or True,
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder)
        _click(win, 0)
        _delete_key(win, shift=True)

        assert confirmations == [["a.mp4"]]
        assert trash_calls == []
        assert sorted(path.name for path in folder.iterdir()) == ["a.mp4", "b.mp4"]
        assert win.model.count() == 2
    finally:
        win.close()


def test_shift_delete_confirm_permanently_deletes_selection(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path)
    confirmations: list[list[str]] = []

    def confirm(parent, filenames) -> bool:
        confirmations.append(list(filenames))
        return True

    monkeypatch.setattr(
        "video_previewer.ui.main_window.delete_dialog.confirm_permanent_delete",
        confirm,
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder)
        _click(win, 0)
        _click(win, 1, Qt.KeyboardModifier.ControlModifier)
        _delete_key(win, shift=True)

        assert confirmations == [["a.mp4", "b.mp4"]]
        assert list(folder.iterdir()) == []
        assert win.model.count() == 0
    finally:
        win.close()


def test_delete_reports_partial_failure_and_keeps_failed_tile(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path)
    warnings: list[str] = []

    def fake_trash(path: Path) -> bool:
        if path.name == "b.mp4":
            return False
        path.unlink()
        return True

    monkeypatch.setattr(
        "video_previewer.ui.main_window.move_to_trash", fake_trash
    )
    monkeypatch.setattr(
        "video_previewer.ui.main_window.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(str(args)),
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder)
        _click(win, 0)
        _click(win, 1, Qt.KeyboardModifier.ControlModifier)
        _delete_key(win)

        assert warnings and "b.mp4" in warnings[0]
        assert not (folder / "a.mp4").exists()
        assert (folder / "b.mp4").exists()
        assert win.model.count() == 1
        assert win.model.item_at(0).filename == "b.mp4"
    finally:
        win.close()


def test_late_scan_result_cannot_resurrect_deleted_video(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path, names=("a.mp4",))

    def fake_trash(path: Path) -> bool:
        path.unlink()
        return True

    monkeypatch.setattr(
        "video_previewer.ui.main_window.move_to_trash", fake_trash
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder, count=1)
        item = win.model.item_at(0)
        key = win._db.scan_key(folder, False)
        _click(win, 0)
        _delete_key(win)
        assert win.model.count() == 0

        win._on_scan_items([item], win._scan_gen)
        win._on_scan_finished(
            ScanResult(
                folder=folder,
                recursive=False,
                fresh={item.path.as_posix(): (item.size, item.modified)},
            ),
            win._scan_gen,
        )

        assert win.model.count() == 0
        assert win._db.load_scan(key) == []
    finally:
        win.close()


def test_delete_without_selection_does_nothing(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path)
    calls: list[Path] = []
    monkeypatch.setattr(
        "video_previewer.ui.main_window.move_to_trash",
        lambda path: calls.append(path) or True,
    )
    win = MainWindow()
    try:
        _open(win, qapp, folder)
        _delete_key(win)
        assert calls == []
        assert win.model.count() == 2
    finally:
        win.close()
