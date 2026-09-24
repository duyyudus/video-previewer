"""Info bar under the grid: total size of the selected videos."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QItemSelectionModel

from conftest import pump
from video_previewer.ui.main_window import MainWindow


def _folder(tmp_path) -> Path:
    folder = tmp_path / "vids"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x" * 1000)
    (folder / "b.mp4").write_bytes(b"x" * 2000)
    (folder / "c.mp4").write_bytes(b"x" * 3000)
    return folder


def _select(win: MainWindow, rows: list[int]) -> None:
    sel = win.grid.selectionModel()
    sel.clear()
    for row in rows:
        sel.select(
            win.model.index(row),
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )


def test_info_bar_shows_selected_total_size(qapp, cache_dir, tmp_path):
    folder = _folder(tmp_path)
    win = MainWindow()
    try:
        win.show()
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 3, timeout=30)
        bar = win.info_bar
        assert pump(qapp, lambda: bar.selection_text == "No videos selected")

        rows = {win.model.item_at(r).filename: r for r in range(3)}
        _select(win, [rows["a.mp4"]])
        assert pump(qapp, lambda: bar.selection_text == "1 video selected · 1000 B")

        _select(win, [rows["b.mp4"], rows["c.mp4"]])
        assert pump(
            qapp, lambda: bar.selection_text == "2 videos selected · 4.9 KB"
        )

        win.grid.clearSelection()
        assert pump(qapp, lambda: bar.selection_text == "No videos selected")
    finally:
        win.close()
