"""MainWindow state handling on folder switch and close (audit M5 + M8)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPointF, QSize, QTimer, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from conftest import pump
from video_previewer import config
from video_previewer.models.video_item import VideoItem
from video_previewer.ui.main_window import MainWindow


def _dummy_folder(tmp_path, name: str = "vids") -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x" * 64)
    (folder / "b.mp4").write_bytes(b"y" * 64)
    return folder


def test_open_folder_resets_stale_hover_state(qapp, cache_dir, tmp_path):
    # M8: _open_folder cleared the model but left the grid's _hover_row
    # pointing at a row index the fresh scan may reuse for a *different*
    # item — the tile under the cursor then neither previewed nor scrubbed
    # until the pointer left and came back.
    folder = _dummy_folder(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        for _ in range(3):
            qapp.processEvents()  # let the grid lay out its cells

        rect = win.grid._cell_rect(0)
        win.grid._on_pointer_move(rect.center())
        assert win.grid._hover_row == 0
        assert win.player.active_path == str(win.model.item_at(0).path)
        # press-based double-click tracking is also row-keyed state
        win.grid._last_press = (0, rect.center(), 0.0)
        win.grid._last_open = (0, 0.0)

        win._open_folder(folder)  # reopening/switching resets everything
        assert win.grid._hover_row == -1
        assert win.player.active_path is None
        assert win.grid._last_press is None
        assert win.grid._last_open is None
    finally:
        win.close()


def test_empty_space_double_click_opens_folder_picker(qapp, cache_dir, tmp_path,
                                                      monkeypatch):
    # Double-clicking empty grid space (no video under the pointer) offers
    # the folder picker, and the picked folder is opened for scanning.
    folder = _dummy_folder(tmp_path)
    monkeypatch.setattr(
        "video_previewer.ui.main_window.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: str(folder),
    )
    win = MainWindow()
    win.show()
    try:
        for _ in range(3):
            qapp.processEvents()
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick,
            QPointF(30, 30),
            QPointF(30, 30),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        win.grid.mouseDoubleClickEvent(event)  # empty model: every spot is empty
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        assert win._folder_label.text() == str(folder)
    finally:
        win.close()


def test_closing_window_ignores_late_mutations(qapp, cache_dir, tmp_path):
    # M5: the closeEvent drain loop keeps the event queue running, so a
    # queued click or late scan result could mutate state mid-close. Once
    # _closing is set, _open_folder and the scan/thumb handlers are no-ops.
    folder = _dummy_folder(tmp_path)
    other = _dummy_folder(tmp_path, "other")
    win = MainWindow()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)

        win._closing = True
        gen = win._scan_gen
        win._open_folder(other)  # e.g. a queued button press during the drain
        assert win._scan_gen == gen
        assert win._folder_label.text() == str(folder)
        assert win.model.count() == 2

        # a scan result for the still-current generation must not land either
        win._on_scan_items([VideoItem(other / "z.mp4", 5, 2.0)], gen)
        assert win.model.count() == 2

        # nor may a dying scan's error, or a late thumbnail, touch the UI
        win._on_scan_error("boom")
        assert "Scan failed" not in win._status_label.text()
        win._on_thumb_ready(win.model.item_at(0).with_thumbnail(Path("/t/a.jpg")))
        assert win.model.item_at(0).thumb_ready is False
    finally:
        win.close()


def test_close_drain_ignores_real_pointer_gestures(qapp, cache_dir, tmp_path,
                                                   monkeypatch):
    # M5, end to end: closeEvent pumps the event queue with the window still
    # on screen. A hover delivered there used to restart the shared player
    # right after shutdown stopped it, and a press pair would have launched an
    # external player mid-close.
    folder = _dummy_folder(tmp_path)
    opened: list[Path] = []
    monkeypatch.setattr(
        "video_previewer.ui.video_grid.open_externally", opened.append
    )

    win = MainWindow()
    win.show()
    win._open_folder(folder)
    assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
    for _ in range(3):
        qapp.processEvents()

    # Sanity: the same gesture works while the window is alive.
    rect = win.grid._cell_rect(1)
    win.grid._on_pointer_move(rect.center())
    assert win.player.active_path == str(win.model.item_at(1).path)

    delivered = {"done": False}

    def hover_and_press() -> None:
        local = QPointF(rect.center())
        glob = QPointF(win.grid.viewport().mapToGlobal(rect.center()))
        for etype, button in (
            (QEvent.Type.MouseMove, Qt.MouseButton.NoButton),
            (QEvent.Type.MouseButtonPress, Qt.MouseButton.LeftButton),
        ):
            QApplication.sendEvent(
                win.grid.viewport(),
                QMouseEvent(etype, local, glob, button,
                            Qt.MouseButton.NoButton,
                            Qt.KeyboardModifier.NoModifier),
            )
        delivered["done"] = True

    # Keep the drain loop pumping until the gesture has been delivered.
    def fake_pending_count() -> int:
        return 0 if delivered["done"] else 1

    QTimer.singleShot(0, hover_and_press)
    monkeypatch.setattr(win._thumbs, "pending_count", fake_pending_count)
    win.close()

    assert delivered["done"], "gesture was never delivered during the drain"
    assert win._closing is True
    assert win.grid._closing is True
    assert win.grid._hover_row == -1
    assert win.player.active_path is None  # the preview stayed stopped
    assert opened == []  # nothing was launched mid-close


def test_scan_cancellation_hook_covers_close(qapp, cache_dir, tmp_path):
    # A scan that is still walking when the window closes must stop at its next
    # cancellation check instead of outliving the database it writes to.
    win = MainWindow()
    try:
        hook = win._scan_stale(7)  # the exact callable the worker thread polls
        assert hook() is True  # generation 7 is not current yet
        win._scan_gen = 7
        assert hook() is False  # current generation, window alive
        win._closing = True
        assert hook() is True  # closing supersedes even the current scan
    finally:
        win.close()


def test_window_geometry_restored_across_instances(qapp, cache_dir, file_settings):
    # The window remembers its geometry across runs: closing persists it, and
    # a fresh MainWindow picks it up — even though the exit decision (discard,
    # via the conftest stub) forgets the folder itself.
    win = MainWindow()
    # 700x500 fits inside the offscreen platform's 800x800 virtual screen;
    # restoreGeometry clamps sizes larger than the screen, so anything
    # bigger would not round-trip exactly.
    win.resize(700, 500)
    win.close()
    win.deleteLater()

    win2 = MainWindow()
    try:
        assert win2.size() == QSize(700, 500)
    finally:
        win2.close()


def test_window_default_size_without_saved_geometry(qapp, cache_dir, file_settings):
    # First run: nothing remembered yet, so the configured default applies.
    win = MainWindow()
    try:
        assert win.size() == QSize(
            config.DEFAULT_WINDOW_WIDTH, config.DEFAULT_WINDOW_HEIGHT
        )
    finally:
        win.close()


def test_window_corrupt_geometry_falls_back_to_default(qapp, cache_dir, file_settings):
    # Fail soft (rule 8): a corrupt blob must not break startup; the window
    # simply opens at the default size.
    from PySide6.QtCore import QSettings

    s = QSettings(str(file_settings), QSettings.Format.IniFormat)
    s.setValue(config.SETTING_WINDOW_GEOMETRY, QByteArray(b"not a geometry blob"))
    s.sync()

    win = MainWindow()
    try:
        assert win.size() == QSize(
            config.DEFAULT_WINDOW_WIDTH, config.DEFAULT_WINDOW_HEIGHT
        )
    finally:
        win.close()
