"""Tile selection (click / Ctrl / rubber band) and the F2 rename action."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPushButton

import sys

import pytest

from conftest import make_video, pump
from video_previewer.cache.cache import ThumbnailCache
from video_previewer.models.video_item import VideoItem
from video_previewer.models.video_model import VideoModel
from video_previewer.ui.main_window import MainWindow
from video_previewer.ui.rename_dialog import build_rename_dialog
from video_previewer.ui.video_grid import VideoGrid


def _dummy_folder(tmp_path, names=("a.mp4", "b.mp4")) -> Path:
    folder = tmp_path / "vids"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"x" * 64)
    return folder


def _click(grid: VideoGrid, pos: QPointF,
           modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    """Press + release on the viewport, the way a real click reaches QListView."""
    for etype in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(
            grid.viewport(),
            QMouseEvent(
                etype, pos, pos,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, modifiers,
            ),
        )


def _key(widget, key: Qt.Key,
         modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    QApplication.sendEvent(widget, QKeyEvent(QEvent.Type.KeyPress, key, modifiers))


def _row_center(win: MainWindow, row: int) -> QPointF:
    for _ in range(3):
        QApplication.processEvents()
    rect = win.grid.visualRect(win.model.index(row))
    assert rect.isValid()
    return QPointF(rect.center())


def _names(win: MainWindow) -> list[str]:
    return [win.model.item_at(r).filename for r in range(win.model.count())]


# -- model flags -------------------------------------------------------------------


def test_items_are_selectable():
    model = VideoModel()
    path = Path("x.mp4")
    model.add_items([VideoItem(path, 10, 1.0)])
    flags = model.flags(model.index(0))
    assert bool(flags & Qt.ItemFlag.ItemIsSelectable)
    assert bool(flags & Qt.ItemFlag.ItemIsEnabled)


# -- grid selection --------------------------------------------------------------------


def test_click_selects_one_tile(qapp, cache_dir, tmp_path):
    folder = _dummy_folder(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        sel = win.grid.selectionModel().selectedRows()
        assert [i.row() for i in sel] == [0]

        _click(win.grid, _row_center(win, 1))  # plain click replaces the selection
        sel = win.grid.selectionModel().selectedRows()
        assert [i.row() for i in sel] == [1]
    finally:
        win.close()


def test_ctrl_click_extends_selection(qapp, cache_dir, tmp_path):
    folder = _dummy_folder(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        _click(win.grid, _row_center(win, 1), Qt.KeyboardModifier.ControlModifier)
        assert len(win.grid.selectionModel().selectedRows()) == 2
    finally:
        win.close()


def test_escape_clears_selection(qapp, cache_dir, tmp_path):
    folder = _dummy_folder(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        assert len(win.grid.selectionModel().selectedRows()) == 1
        _key(win.grid, Qt.Key.Key_Escape)
        assert len(win.grid.selectionModel().selectedRows()) == 0
    finally:
        win.close()


# -- rename dialog (stem edit, extension fixed) -----------------------------------------


def test_rename_dialog_prefills_stem_only(qapp):
    dlg, edit = build_rename_dialog(None, "holiday movie.mp4")
    assert edit.text() == "holiday movie"
    assert dlg.findChild(QDialogButtonBox) is not None
    ok = dlg.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok)
    assert isinstance(ok, QPushButton)

    edit.setText("")
    assert not ok.isEnabled()
    edit.setText("bad<name>/x")  # illegal on Windows
    assert not ok.isEnabled()
    edit.setText("trailing dot.")
    assert not ok.isEnabled()
    edit.setText("good name")
    assert ok.isEnabled()
    dlg.deleteLater()


# -- F2 rename end to end -------------------------------------------------------------


def _stub_dialog(monkeypatch, stem: str | None) -> list:
    calls: list[str] = []

    def fake(parent, filename):
        calls.append(filename)
        return stem

    monkeypatch.setattr(
        "video_previewer.ui.rename_dialog.ask_new_stem", fake
    )
    return calls


def test_f2_renames_selected_video(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)  # a.mp4, b.mp4
    calls = _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))  # select a.mp4
        _key(win.grid, Qt.Key.Key_F2)

        assert calls == ["a.mp4"]
        assert (folder / "z.mp4").exists()
        assert not (folder / "a.mp4").exists()
        # name sort: z lands last, and the selection follows the renamed tile
        assert _names(win) == ["b.mp4", "z.mp4"]
        sel = win.grid.selectionModel().selectedRows()
        assert len(sel) == 1
        assert win.model.item_at(sel[0].row()).filename == "z.mp4"
    finally:
        win.close()


def test_f2_without_selection_does_nothing(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)
    calls = _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _key(win.grid, Qt.Key.Key_F2)
        assert calls == []
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4", "b.mp4"]
    finally:
        win.close()


def test_f2_with_multiple_selection_does_nothing(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path)
    calls = _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        _click(win.grid, _row_center(win, 1), Qt.KeyboardModifier.ControlModifier)
        _key(win.grid, Qt.Key.Key_F2)
        assert calls == []
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4", "b.mp4"]
    finally:
        win.close()


def test_rename_cancel_keeps_everything(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)
    _stub_dialog(monkeypatch, None)  # dialog cancelled
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4", "b.mp4"]
        assert _names(win) == ["a.mp4", "b.mp4"]
    finally:
        win.close()


def test_rename_collision_warns_and_keeps_file(
    qapp, cache_dir, tmp_path, monkeypatch
):
    folder = _dummy_folder(tmp_path)
    _stub_dialog(monkeypatch, "b")  # b.mp4 already exists
    warnings: list[str] = []
    monkeypatch.setattr(
        "video_previewer.ui.main_window.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(str(args)),
    )
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)
        assert warnings
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4", "b.mp4"]
        assert _names(win) == ["a.mp4", "b.mp4"]
    finally:
        win.close()


def test_rename_stops_hover_preview(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)
    _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        center = _row_center(win, 0)
        win.grid._on_pointer_move(center.toPoint())
        assert win.player.active_path is not None

        _click(win.grid, center)
        _key(win.grid, Qt.Key.Key_F2)
        assert win.player.active_path is None
    finally:
        win.close()


# -- cache migration (rule 4: a rename must not re-burn ffmpeg) -------------------------


def test_rename_migrates_thumbnail_cache(qapp, cache_dir, tmp_path, monkeypatch):
    folder = tmp_path / "vids"
    folder.mkdir()
    make_video(folder / "a.mp4")
    make_video(folder / "b.mp4")
    _stub_dialog(monkeypatch, "renamed")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        assert pump(
            qapp,
            lambda: all(
                win.model.item_at(r).thumb_ready for r in range(win.model.count())
            ),
            timeout=60,
        )
        old_vid = win.model.item_at(0).vid

        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)

        new_path = folder / "renamed.mp4"
        st = new_path.stat()
        new_item = VideoItem(new_path, st.st_size, st.st_mtime)
        row = win._db.get_video(new_item.vid)
        assert row is not None  # metadata moved to the new identity
        assert Path(row.thumbnail).exists()  # JPEG relocated, not re-extracted
        assert win._db.get_video(old_vid) is None  # old identity retired
        # the tile keeps its thumbnail in place
        assert win.model.item_at(0).thumb_ready
        assert Path(win.model.item_at(0).thumbnail_path).exists()
    finally:
        win.close()


def test_rename_updates_cached_scan_entries(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)
    _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        key = win._db.scan_key(folder, False)
        assert pump(qapp, lambda: win._db.load_scan(key) is not None, timeout=30)

        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)

        entries = win._db.load_scan(key)
        paths = sorted(Path(e["path"]).name for e in entries)
        assert paths == ["b.mp4", "z.mp4"]  # no ghost of the old name
    finally:
        win.close()

# -- selection is visible ------------------------------------------------------------


def _paint_tile(model, row: int, selected: bool):
    """Render one tile through the delegate into a QImage."""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter
    from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

    from video_previewer.ui.video_delegate import VideoDelegate

    image = QImage(220, 160, QImage.Format.Format_RGB32)
    image.fill(QColor("black"))
    painter = QPainter(image)
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, 220, 160)
    option.font = QApplication.instance().font()
    option.state = QStyle.StateFlag.State_Enabled
    if selected:
        option.state |= QStyle.StateFlag.State_Selected
    VideoDelegate().paint(painter, option, model.index(row))
    painter.end()
    return image


def _rich_jpeg(tmp_path) -> Path:
    """A real multi-color JPEG to stand in for a thumbnail."""
    from PySide6.QtGui import QColor, QPainter, QPixmap

    pix = QPixmap(320, 180)
    p = QPainter(pix)
    for i in range(0, 320, 8):
        p.fillRect(i, 0, 8, 180, QColor(i, (i * 3) % 256, 128))
    p.end()
    path = tmp_path / "thumb.jpg"
    assert pix.save(str(path), "JPG")
    return path


def _img_area_colors(image, x0=20, y0=40, w=180, h=60) -> int:
    """Unique colors in the tile's image area (where the thumbnail lives)."""
    region = image.copy(x0, y0, w, h)
    colors = set()
    for y in range(0, h, 3):
        for x in range(0, w, 3):
            colors.add(region.pixel(x, y))
    return len(colors)


def test_selected_tile_paints_highlight(qapp, tmp_path):
    model = VideoModel()
    item = VideoItem(Path("/v/a.mp4"), 10, 1.0).with_thumbnail(_rich_jpeg(tmp_path))
    model.add_items([item])
    plain = _paint_tile(model, 0, selected=False)
    lit = _paint_tile(model, 0, selected=True)
    # the selection ring must actually change pixels
    assert any(
        plain.pixel(x, y) != lit.pixel(x, y)
        for x in range(0, 220, 4)
        for y in range(0, 160, 4)
    )
    # ...without hiding the thumbnail: the image area of a SELECTED tile
    # must stay as colorful as an unselected one (a regression painted a
    # flat opaque fill over the whole card).
    assert _img_area_colors(plain) > 20
    assert _img_area_colors(lit) > 20


def test_cache_rename_survives_case_only_rename(cache_dir, monkeypatch):
    # Windows video_id() lowercases the path, so 'a.mp4' -> 'A.mp4' keeps
    # the SAME vid. cache.rename must update that row in place, not delete
    # the row it just wrote (and the JPEG is already at the right path).
    from video_previewer.cache.database import Database
    from video_previewer.models import video_item

    db = Database(cache_dir / "metadata.sqlite")
    monkeypatch.setattr(video_item, "_SYSTEM", "Windows")
    old = VideoItem(Path("C:/v/a.mp4"), 10, 1.0)
    new = VideoItem(Path("C:/v/A.mp4"), 10, 1.0)
    assert old.vid == new.vid
    thumb = cache_dir / "thumbnails" / f"{old.vid}.jpg"
    thumb.parent.mkdir(parents=True, exist_ok=True)
    thumb.write_bytes(b"jpeg")
    cache = ThumbnailCache(db)
    cache.store(old, thumb, 1000, 320, 180, "h264")

    result = cache.rename(old, new)

    row = db.get_video(new.vid)
    assert row is not None  # row kept, not deleted after upsert
    assert row.path == "C:/v/A.mp4"
    assert row.duration_ms == 1000
    assert result == thumb and thumb.exists()
    db.close()



@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive filesystem")
def test_f2_case_only_rename_allowed_on_windows(qapp, cache_dir, tmp_path,
                                                monkeypatch):
    # 'a.mp4' -> 'A.mp4' is a legal rename on Windows even though
    # new_path.exists() is True for the same file.
    folder = _dummy_folder(tmp_path)
    _stub_dialog(monkeypatch, "A")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)
        # exists() can't tell the case on a case-insensitive FS: list the
        # real on-disk names instead.
        assert sorted(p.name for p in folder.iterdir()) == ["A.mp4", "b.mp4"]
    finally:
        win.close()


# -- rename racing the scan that filled the grid ----------------------------------------


def test_stale_scan_finish_keeps_renamed_thumbnail(qapp, cache_dir, tmp_path,
                                                   monkeypatch):
    # The scan's fresh-file list is captured while walking; purge runs when
    # the scan finishes. A rename in between must not make the purge treat
    # the renamed file as deleted (it deletes the migrated row + JPEG).
    folder = tmp_path / "vids"
    folder.mkdir()
    make_video(folder / "a.mp4")
    make_video(folder / "b.mp4")
    st_before = (folder / "a.mp4").stat()
    fresh_old = {  # what the walk saw: the OLD name
        (folder / "a.mp4").as_posix(): (st_before.st_size, st_before.st_mtime),
        (folder / "b.mp4").as_posix(): None,
    }
    sb = (folder / "b.mp4").stat()
    fresh_old[(folder / "b.mp4").as_posix()] = (sb.st_size, sb.st_mtime)

    _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        assert pump(
            qapp,
            lambda: all(
                win.model.item_at(r).thumb_ready for r in range(win.model.count())
            ),
            timeout=60,
        )
        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)

        # queued ``finished`` from the scan that started BEFORE the rename
        from video_previewer.workers.scanner import ScanResult

        # The in-flight worker's save_scan lands AFTER the rename (it
        # captured its entries while walking): replay it with the OLD path
        # so the finish-time repair is what has to fix the scan row —
        # the rename-time patch is already overwritten here.
        sb = (folder / "b.mp4").stat()
        win._db.save_scan(win._db.scan_key(folder, False), [
            {"path": (folder / "a.mp4").as_posix(),
             "size": st_before.st_size, "mtime": st_before.st_mtime},
            {"path": (folder / "b.mp4").as_posix(),
             "size": sb.st_size, "mtime": sb.st_mtime},
        ])

        win._on_scan_finished(
            ScanResult(folder=folder, recursive=False, fresh=fresh_old),
            win._scan_gen,
        )
        row = win.model.row_for(folder / "z.mp4")
        assert row is not None
        item = win.model.item_at(row)
        assert item.thumb_ready and Path(item.thumbnail_path).exists()
        assert win._db.get_video(item.vid) is not None
        # the worker's save_scan (old paths) may have landed after the
        # rename-time patch: finishing must repair the scan row too
        entries = win._db.load_scan(win._db.scan_key(folder, False))
        assert entries is not None
        assert sorted(Path(e["path"]).name for e in entries) == ["b.mp4", "z.mp4"]
    finally:
        win.close()


def test_renamed_away_path_is_not_readded(qapp, cache_dir, tmp_path, monkeypatch):
    # A stale scan batch still carrying the OLD path must not re-add a dead
    # ghost tile after the rename.
    folder = _dummy_folder(tmp_path)
    _stub_dialog(monkeypatch, "z")
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _click(win.grid, _row_center(win, 0))
        _key(win.grid, Qt.Key.Key_F2)

        old = VideoItem(folder / "a.mp4", 64, 1_600_000_000.0)
        win._on_scan_items([old], win._scan_gen)
        assert win.model.count() == 2
        assert win.model.row_for(folder / "a.mp4") is None
    finally:
        win.close()
