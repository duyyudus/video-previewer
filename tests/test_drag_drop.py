"""Drag selected tiles out: strip presses scrub instead of dragging,
sidebar drops move files (cache + scan migrate), and an executed external
move reconciles the grid."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QModelIndex, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QMouseEvent,
)
from PySide6.QtWidgets import QApplication

from conftest import make_video, pump
from video_previewer.models.video_item import VideoItem
from video_previewer.models.video_model import VideoModel
from video_previewer.ui import video_grid as video_grid_mod
from video_previewer.ui.folder_sidebar import FolderSidebar
from video_previewer.ui.main_window import MainWindow


def _dummy_folder(tmp_path, names=("a.mp4", "b.mp4")) -> Path:
    folder = tmp_path / "vids"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"x" * 64)
    return folder


def _press_release(grid, pos: QPointF) -> None:
    """Press + release on the viewport, the way a real click reaches QListView."""
    for etype in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonRelease):
        QApplication.sendEvent(
            grid.viewport(),
            QMouseEvent(
                etype, pos, pos,
                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )


def _press_drag(grid, start: QPointF, offset_x: float = 80.0) -> None:
    """Press at *start* then sweep right, well past the start-drag distance.

    This is the exact gesture Qt turns into a drag (SelectingState +
    ``canStartDrag`` + distance exceeded), so it exercises the real
    scrub-vs-drag decision instead of calling ``startDrag`` directly.
    """
    QApplication.sendEvent(
        grid.viewport(),
        QMouseEvent(
            QEvent.Type.MouseButtonPress, start, start,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    for frac in (0.3, 0.6, 1.0):
        pos = QPointF(start.x() + offset_x * frac, start.y())
        QApplication.sendEvent(
            grid.viewport(),
            QMouseEvent(
                QEvent.Type.MouseMove, pos, pos,
                Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            ),
        )
    QApplication.sendEvent(
        grid.viewport(),
        QMouseEvent(
            QEvent.Type.MouseButtonRelease,
            QPointF(start.x() + offset_x, start.y()),
            QPointF(start.x() + offset_x, start.y()),
            Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        ),
    )


def _row_center(win: MainWindow, row: int) -> QPointF:
    for _ in range(3):
        QApplication.processEvents()
    rect = win.grid.visualRect(win.model.index(row))
    assert rect.isValid()
    return QPointF(rect.center())


def _strip_pos(win: MainWindow, row: int) -> QPointF:
    """A point inside the timeline strip at the bottom of *row*'s card."""
    cell = win.grid._cell_rect(row)
    assert cell.isValid()
    return QPointF(cell.center().x(), cell.bottom() - 2)


class _FakeDrag:
    """Stand-in for QDrag: the offscreen platform has no real drag loop."""

    created: list["_FakeDrag"] = []
    result = Qt.DropAction.CopyAction
    on_exec = None  # optional callback(source) run while the drag "runs"

    def __init__(self, source) -> None:
        self.source = source
        self.mime: QMimeData | None = None
        self.executed: tuple | None = None
        _FakeDrag.created.append(self)

    def setMimeData(self, mime) -> None:  # noqa: N802
        self.mime = mime

    def setPixmap(self, pixmap) -> None:  # noqa: N802
        pass

    def setHotSpot(self, pos) -> None:  # noqa: N802
        pass

    def exec(self, actions, default) -> Qt.DropAction:
        self.executed = (actions, default)
        if _FakeDrag.on_exec is not None:
            _FakeDrag.on_exec(self.source)
        return _FakeDrag.result


def _install_fake_drag(monkeypatch) -> type[_FakeDrag]:
    _FakeDrag.created = []
    _FakeDrag.result = Qt.DropAction.CopyAction
    _FakeDrag.on_exec = None
    monkeypatch.setattr(video_grid_mod, "QDrag", _FakeDrag)
    # Belt and brace: press pairs must never launch the external player here.
    monkeypatch.setattr(video_grid_mod, "open_externally", lambda path: None)
    return _FakeDrag


# -- model: drag flags, URL payload, row removal -----------------------------------------


def test_items_are_draggable_and_carry_file_urls():
    model = VideoModel()
    model.add_items([VideoItem(Path("/v/a.mp4"), 10, 1.0),
                     VideoItem(Path("/v/b.mp4"), 10, 1.0)])
    flags = model.flags(model.index(0))
    assert bool(flags & Qt.ItemFlag.ItemIsDragEnabled)
    mime = model.mimeData([model.index(0), model.index(1)])
    assert mime.hasUrls()
    assert [u.toLocalFile() for u in mime.urls()] == ["/v/a.mp4", "/v/b.mp4"]


def test_remove_items_drops_rows_and_reindexes():
    model = VideoModel()
    model.add_items([VideoItem(Path(f"/v/{n}.mp4"), 10, 1.0) for n in "abcde"])
    # a hole, a gap, and an unknown path
    assert model.remove_items(
        [Path("/v/b.mp4"), Path("/v/d.mp4"), Path("/v/zz.mp4")]
    ) == 2
    assert [model.item_at(r).filename for r in range(model.count())] == [
        "a.mp4", "c.mp4", "e.mp4",
    ]
    assert model.row_for(Path("/v/a.mp4")) == 0
    assert model.row_for(Path("/v/c.mp4")) == 1
    assert model.row_for(Path("/v/e.mp4")) == 2
    # a contiguous block
    assert model.remove_items([Path("/v/a.mp4"), Path("/v/c.mp4")]) == 2
    assert [model.item_at(r).filename for r in range(model.count())] == ["e.mp4"]
    assert model.remove_items([]) == 0


# -- grid: scrub presses never drag, card presses do --------------------------------------


def test_strip_press_never_starts_a_drag(qapp, cache_dir, tmp_path, monkeypatch):
    folder = _dummy_folder(tmp_path)
    fake = _install_fake_drag(monkeypatch)
    monkeypatch.setattr(video_grid_mod, "_IS_WINDOWS", True)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)

        # Press-hold on the timeline strip + horizontal sweep is the scrub
        # gesture: the real Qt gesture path reaches the drag decision, and
        # the grid must swallow it.
        _press_drag(win.grid, _strip_pos(win, 0))
        qapp.processEvents()
        assert fake.created == []

        # Press on the card body + sweep is a drag: it carries the selection
        # as real file URLs, Move-first.
        _press_drag(win.grid, _row_center(win, 0))
        qapp.processEvents()
        assert len(fake.created) == 1
        drag = fake.created[0]
        assert [Path(u.toLocalFile()) for u in drag.mime.urls()] == [
            folder / "a.mp4"
        ]
        assert drag.mime.data(video_grid_mod._PREFERRED_DROP_EFFECT) == (
            video_grid_mod._DROPEFFECT_MOVE
        )
        assert drag.executed == (
            Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
            Qt.DropAction.MoveAction,
        )
    finally:
        win.close()


def test_empty_space_press_never_starts_a_drag(qapp, cache_dir, tmp_path,
                                               monkeypatch):
    # A rubber-band drag starts on empty space; the base canStartDrag falls
    # back to the current index there, which would hijack it into dragging
    # the leftover selection.
    folder = _dummy_folder(tmp_path)
    fake = _install_fake_drag(monkeypatch)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _press_release(win.grid, _row_center(win, 0))  # select a tile
        win._model.clear()  # the whole viewport is empty space now
        qapp.processEvents()
        _press_drag(win.grid, QPointF(30, 30))
        qapp.processEvents()
        assert fake.created == []
    finally:
        win.close()


def test_external_move_drops_tiles_by_existence(qapp, cache_dir, tmp_path,
                                                monkeypatch):
    # A file manager (Finder/Explorer) moved the file out itself. The
    # returned drag action is unreliable across platforms, so the window
    # reconciles by existence: dragged paths that vanished lose their tile,
    # and their scan-cache entry goes too (no ghost on reopen).
    folder = _dummy_folder(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = _install_fake_drag(monkeypatch)

    def move_out(_source) -> None:  # what the file manager does on drop
        (folder / "a.mp4").rename(elsewhere / "a.mp4")

    fake.on_exec = move_out
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        key = win._db.scan_key(folder, False)
        assert pump(qapp, lambda: win._db.load_scan(key) is not None, timeout=30)

        _press_drag(win.grid, _row_center(win, 0))

        assert win.model.count() == 1
        assert win.model.item_at(0).filename == "b.mp4"
        entries = win._db.load_scan(key)
        assert [Path(e["path"]).name for e in entries] == ["b.mp4"]
    finally:
        win.close()


def test_copy_out_keeps_tiles(qapp, cache_dir, tmp_path, monkeypatch):
    # A copy leaves the originals on disk: reconciliation finds them all
    # present and the grid stays untouched.
    folder = _dummy_folder(tmp_path)
    _install_fake_drag(monkeypatch)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _press_drag(win.grid, _row_center(win, 0))
        assert win.model.count() == 2
    finally:
        win.close()


def test_move_action_deletes_the_originals(qapp, cache_dir, tmp_path, monkeypatch):
    # Explorer may copy the files into the drop folder and return MoveAction,
    # leaving the source app to delete the originals. Without that final step
    # the drag-out leaves a duplicate behind.
    folder = _dummy_folder(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = _install_fake_drag(monkeypatch)
    fake.result = Qt.DropAction.MoveAction

    def copy_out(_source) -> None:  # what Explorer does on drop
        shutil.copy2(folder / "a.mp4", elsewhere / "a.mp4")

    fake.on_exec = copy_out
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        key = win._db.scan_key(folder, False)
        assert pump(qapp, lambda: win._db.load_scan(key) is not None, timeout=30)

        _press_drag(win.grid, _row_center(win, 0))

        assert not (folder / "a.mp4").exists()  # the move is completed
        assert (elsewhere / "a.mp4").exists()  # Explorer's copy is untouched
        assert win.model.count() == 1
        assert win.model.item_at(0).filename == "b.mp4"
        entries = win._db.load_scan(key)
        assert [Path(e["path"]).name for e in entries] == ["b.mp4"]
    finally:
        win.close()


def test_copy_out_never_deletes_the_originals(qapp, cache_dir, tmp_path, monkeypatch):
    # Ctrl-drag (or Explorer simply deciding on a copy) comes back as
    # CopyAction: the originals must survive. Only MoveAction asks the source
    # to delete a file.
    folder = _dummy_folder(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = _install_fake_drag(monkeypatch)
    fake.result = Qt.DropAction.CopyAction
    fake.on_exec = lambda _source: shutil.copy2(
        folder / "a.mp4", elsewhere / "a.mp4"
    )
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _press_drag(win.grid, _row_center(win, 0))
        assert (folder / "a.mp4").exists()
        assert win.model.count() == 2
    finally:
        win.close()


def test_target_move_never_deletes_the_originals(qapp, cache_dir, tmp_path,
                                                 monkeypatch):
    # On Windows, TargetMoveAction transfers ownership to the target and
    # explicitly tells the source not to delete its data.
    folder = _dummy_folder(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    fake = _install_fake_drag(monkeypatch)
    fake.result = Qt.DropAction.TargetMoveAction
    fake.on_exec = lambda _source: shutil.copy2(
        folder / "a.mp4", elsewhere / "a.mp4"
    )
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        _press_drag(win.grid, _row_center(win, 0))
        assert (folder / "a.mp4").exists()
        assert win.model.count() == 2
    finally:
        win.close()


def test_sidebar_drop_is_never_deleted_as_an_external_move(
    qapp, cache_dir, tmp_path, monkeypatch
):
    # The sidebar handled the drop itself (the file already lives in the
    # target folder). Even if the platform then reports MoveAction,
    # the window must not "complete" anything: the moved file would be
    # deleted out from under the user.
    root = tmp_path / "root"
    vids = root / "vids"
    other = root / "other"
    vids.mkdir(parents=True)
    other.mkdir()
    (vids / "a.mp4").write_bytes(b"x" * 64)
    (vids / "b.mp4").write_bytes(b"y" * 64)
    fake = _install_fake_drag(monkeypatch)
    fake.result = Qt.DropAction.MoveAction
    win = MainWindow()
    win.show()
    try:
        win._open_folder(vids)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        fake.on_exec = lambda _source: win.sidebar.files_dropped.emit(
            [str(vids / "a.mp4")], str(other)
        )
        _press_drag(win.grid, _row_center(win, 0))

        assert (other / "a.mp4").exists()  # the sidebar move survives
        assert win.model.count() == 1
    finally:
        win.close()


# -- sidebar: drop-target gating -----------------------------------------------------------


def _sidebar_with_alpha(qapp, tmp_path) -> tuple[FolderSidebar, Path]:
    root = tmp_path / "tree"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    sidebar = FolderSidebar()
    sidebar.set_root(root)
    sidebar.resize(200, 300)
    sidebar.show()
    # rows land one event loop behind the model fetch; the drop position
    # tests need a real row rect
    assert pump(
        qapp,
        lambda: sidebar.visualRect(sidebar._fs_model.index(str(alpha))).height() > 0,
        timeout=30,
    )
    return sidebar, alpha


def _drop_event(sidebar, pos: QPointF, urls,
                action=Qt.DropAction.MoveAction) -> QDropEvent:
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(u)) for u in urls])
    ev = QDropEvent(
        pos, action, mime,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
    )
    # A drop event only *borrows* its payload (a real drag owns it); pin the
    # mime data to the event or Python frees it while the event is in use.
    ev._mime = mime
    return ev


def _empty_pos(sidebar) -> QPointF:
    return QPointF(10, sidebar.viewport().height() - 2)


def test_sidebar_drop_reports_folder_and_files(qapp, tmp_path):
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    received: list = []
    sidebar.files_dropped.connect(lambda paths, folder: received.append((paths, folder)))
    try:
        index = sidebar._fs_model.index(str(alpha))
        ev = _drop_event(
            sidebar, QPointF(sidebar.visualRect(index).center()),
            [Path("/tmp/x.mp4"), Path("/tmp/y.mp4")],
        )
        sidebar.dropEvent(ev)
        assert ev.isAccepted()
        assert len(received) == 1
        paths, folder = received[0]
        assert paths == ["/tmp/x.mp4", "/tmp/y.mp4"]
        assert Path(folder) == alpha
        assert not sidebar._drop_row.isValid()  # highlight cleared after drop
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


def test_sidebar_refuses_drops_it_should_not_move(qapp, tmp_path):
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    received: list = []
    sidebar.files_dropped.connect(lambda paths, folder: received.append(paths))
    try:
        index = sidebar._fs_model.index(str(alpha))

        # empty tree space (no folder row under the pointer)
        ev = _drop_event(sidebar, _empty_pos(sidebar), [Path("/tmp/x.mp4")])
        ev.accept()  # prove the handler can retract acceptance
        sidebar.dropEvent(ev)
        assert not ev.isAccepted()

        # remote URLs are not files we can move
        mime = QMimeData()
        mime.setUrls([QUrl("https://example.com/x.mp4")])
        ev = QDropEvent(
            QPointF(sidebar.visualRect(index).center()),
            Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        ev.accept()
        sidebar.dropEvent(ev)
        assert not ev.isAccepted()

        assert received == []
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


def test_sidebar_drag_enter_gate(qapp, tmp_path):
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/x.mp4")])
        index = sidebar._fs_model.index(str(alpha))

        def enter(pos, action):
            ev = QDragEnterEvent(
                QPoint(pos), action, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            sidebar.dragEnterEvent(ev)
            return ev.isAccepted()

        # a Move over a folder row: accepted (what the grid's drag proposes)
        assert enter(sidebar.visualRect(index).center(), Qt.DropAction.MoveAction)
        # empty tree space: refused
        assert not enter(_empty_pos(sidebar).toPoint(), Qt.DropAction.MoveAction)
        # a Copy proposal: refused, the sidebar only ever moves files
        assert not enter(
            sidebar.visualRect(index).center(), Qt.DropAction.CopyAction
        )
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


def test_sidebar_highlight_tracks_drag(qapp, tmp_path):
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/x.mp4")])
        index = sidebar._fs_model.index(str(alpha))

        def enter(pos, action=Qt.DropAction.MoveAction) -> None:
            ev = QDragEnterEvent(
                QPoint(pos), action, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            sidebar.dragEnterEvent(ev)

        # over the folder row: the would-be target is highlighted
        sidebar._set_drop_row(QModelIndex())
        plain = sidebar.viewport().grab().toImage()
        enter(sidebar.visualRect(index).center())
        lit = sidebar.viewport().grab().toImage()  # paints the highlight branch
        assert plain != lit
        assert sidebar._drop_row.isValid()
        assert sidebar._drop_row.data() == alpha.name

        # sliding off the row retracts the highlight
        enter(_empty_pos(sidebar).toPoint())
        assert not sidebar._drop_row.isValid()

        # a Copy proposal never highlights
        enter(sidebar.visualRect(index).center(), Qt.DropAction.CopyAction)
        assert not sidebar._drop_row.isValid()

        # leaving the tree clears it
        enter(sidebar.visualRect(index).center())
        assert sidebar._drop_row.isValid()
        sidebar.dragLeaveEvent(QDragLeaveEvent())
        assert not sidebar._drop_row.isValid()
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


def test_drag_enter_debug_logging_keeps_gate(qapp, tmp_path, monkeypatch):
    # With the pointer-debug flag on, dragEnterEvent's log line must not
    # throw: PySide aborts the rest of the override on a Python exception,
    # which silently kills the whole drop gate (no accept, no highlight).
    from video_previewer.ui import folder_sidebar

    monkeypatch.setattr(folder_sidebar, "_DEBUG_POINTER", True)
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/x.mp4")])
        index = sidebar._fs_model.index(str(alpha))
        ev = QDragEnterEvent(
            QPoint(sidebar.visualRect(index).center()),
            Qt.DropAction.MoveAction, mime,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
        )
        sidebar.dragEnterEvent(ev)
        assert ev.isAccepted()
        assert sidebar._drop_row.isValid()
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


class _FakeDragEvent:
    """Minimal duck-typed drag event for exercising the sidebar gate."""

    def __init__(self, pos, urls, proposed, possible, source=None):
        self._pos = QPointF(pos)
        self._mime = QMimeData()
        self._mime.setUrls([QUrl.fromLocalFile(str(u)) for u in urls])
        self._proposed = proposed
        self._possible = possible
        self._source = source

    def proposedAction(self):  # noqa: N802
        return self._proposed

    def possibleActions(self):  # noqa: N802
        return self._possible

    def source(self):
        return self._source

    def mimeData(self):  # noqa: N802
        return self._mime

    def position(self):
        return self._pos


def test_drag_move_phantom_position_keeps_target(qapp, tmp_path):
    # Multi-URL drags run through a native macOS drag session whose move
    # events can carry a stale position (the phantom-move quirk the grid
    # also fights): a missed move inside the tree must keep the last
    # armed target highlighted and the drop offered. The drop event
    # re-validates against the real pointer, so releasing over empty
    # space still moves nothing.
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    try:
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("/tmp/x.mp4")])
        index = sidebar._fs_model.index(str(alpha))

        def move(pos):
            ev = QDragMoveEvent(
                QPoint(pos), Qt.DropAction.MoveAction, mime,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            sidebar.dragMoveEvent(ev)
            return ev

        ev = move(sidebar.visualRect(index).center())
        assert ev.isAccepted() and sidebar._drop_row.isValid()

        # a phantom position mapping to empty space: target stays armed
        ev = move(_empty_pos(sidebar).toPoint())
        assert ev.isAccepted() and sidebar._drop_row.isValid()

        # but a real drop over that empty space is still refused
        ev2 = _drop_event(sidebar, _empty_pos(sidebar), ["/tmp/x.mp4"])
        ev2.accept()
        sidebar.dropEvent(ev2)
        assert not ev2.isAccepted()
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


def test_in_app_drag_gate_ignores_copy_proposal(qapp, tmp_path):
    # macOS routes multi-URL drags through a native drag session whose
    # enter events arrive with Copy proposed even though the grid dragged
    # with Move as the default: an in-app source with Move possible must
    # still register as a drop hit (that is what restores the highlight —
    # and the drop — for drags of two or more tiles).
    sidebar, alpha = _sidebar_with_alpha(qapp, tmp_path)
    try:
        from PySide6.QtCore import QObject

        grid = QObject()  # stand-in for the grid's QDrag source object
        index = sidebar._fs_model.index(str(alpha))
        pos = sidebar.visualRect(index).center()

        fake = _FakeDragEvent(
            pos, ["/tmp/a.mp4", "/tmp/b.mp4"],
            Qt.DropAction.CopyAction,
            Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
            source=grid,
        )
        folder, hit = sidebar._drop_hit(fake)
        assert folder is not None
        assert Path(folder) == alpha
        assert hit.isValid()

        # An external drag (no in-app source) proposing Copy stays refused:
        # moving where a copy was offered would eat the source files.
        fake = _FakeDragEvent(
            pos, ["/tmp/a.mp4"],
            Qt.DropAction.CopyAction,
            Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
        )
        folder, hit = sidebar._drop_hit(fake)
        assert folder is None and not hit.isValid()
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


# -- window: sidebar drop moves files end to end --------------------------------------------


def test_sidebar_drop_moves_file_and_drops_tile(qapp, cache_dir, tmp_path):
    root = tmp_path / "root"
    vids = root / "vids"
    other = root / "other"
    vids.mkdir(parents=True)
    other.mkdir()
    (vids / "a.mp4").write_bytes(b"x" * 64)
    (vids / "b.mp4").write_bytes(b"y" * 64)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(vids)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)

        win.sidebar.files_dropped.emit([str(vids / "a.mp4")], str(other))
        qapp.processEvents()

        assert (other / "a.mp4").exists() and not (vids / "a.mp4").exists()
        assert win.model.count() == 1  # the file left the folder: tile gone
        assert win.model.item_at(0).filename == "b.mp4"
    finally:
        win.close()


def test_drop_on_own_folder_is_a_noop(qapp, cache_dir, tmp_path):
    folder = _dummy_folder(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        win.sidebar.files_dropped.emit([str(folder / "a.mp4")], str(folder))
        qapp.processEvents()
        assert sorted(p.name for p in folder.iterdir()) == ["a.mp4", "b.mp4"]
        assert win.model.count() == 2
    finally:
        win.close()


def test_drop_collision_warns_and_keeps(qapp, cache_dir, tmp_path, monkeypatch):
    root = tmp_path / "root"
    vids = root / "vids"
    other = root / "other"
    vids.mkdir(parents=True)
    other.mkdir()
    (vids / "a.mp4").write_bytes(b"x" * 64)
    (vids / "b.mp4").write_bytes(b"y" * 64)
    (other / "b.mp4").write_bytes(b"taken")
    warnings: list[str] = []
    monkeypatch.setattr(
        "video_previewer.ui.main_window.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(str(args)),
    )
    win = MainWindow()
    win.show()
    try:
        win._open_folder(vids)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        win.sidebar.files_dropped.emit(
            [str(vids / "a.mp4"), str(vids / "b.mp4")], str(other)
        )
        qapp.processEvents()
        assert (other / "a.mp4").exists()  # the clear one moved
        assert (vids / "b.mp4").exists()  # the clash stayed put
        assert (other / "b.mp4").read_bytes() == b"taken"  # untouched
        assert warnings
        assert win.model.count() == 1
    finally:
        win.close()


def test_move_into_subfolder_keeps_tile_and_migrates_cache(
    qapp, cache_dir, tmp_path
):
    # Recursive scan: a move into a subfolder stays inside the grid's scope,
    # so the tile follows the file AND the cached row + JPEG migrate with it
    # (rule 4: a move must not re-burn ffmpeg). The scan cache is patched in
    # place (the file is still under the folder).
    vids = tmp_path / "vids"
    sub = vids / "sub"
    vids.mkdir()
    sub.mkdir()
    make_video(vids / "a.mp4")
    win = MainWindow()
    win.show()
    try:
        win._recursive_chk.setChecked(True)
        win._open_folder(vids)
        assert pump(qapp, lambda: win.model.count() == 1, timeout=30)
        assert pump(qapp, lambda: win.model.item_at(0).thumb_ready, timeout=60)
        old_vid = win.model.item_at(0).vid
        key = win._db.scan_key(vids, True)
        assert pump(qapp, lambda: win._db.load_scan(key) is not None, timeout=30)

        win.sidebar.files_dropped.emit([str(vids / "a.mp4")], str(sub))
        qapp.processEvents()

        new_path = sub / "a.mp4"
        assert new_path.exists() and not (vids / "a.mp4").exists()
        assert win.model.count() == 1
        item = win.model.item_at(0)
        assert item.path == new_path
        assert item.thumb_ready and Path(item.thumbnail_path).exists()
        st = new_path.stat()
        new_vid = VideoItem(new_path, st.st_size, st.st_mtime).vid
        assert win._db.get_video(new_vid) is not None  # row at new identity
        assert win._db.get_video(old_vid) is None  # old identity retired
        entries = win._db.load_scan(key)
        assert [Path(e["path"]) for e in entries] == [new_path]
    finally:
        win.close()


def test_move_out_clears_scan_cache_entry(qapp, cache_dir, tmp_path):
    # A move to a folder outside the grid's scope must drop the entry from
    # the scan cache (there is no replacement path to write in this
    # folder's place) — otherwise every reopen replays a ghost tile.
    root = tmp_path / "root"
    vids = root / "vids"
    other = root / "other"
    vids.mkdir(parents=True)
    other.mkdir()
    (vids / "a.mp4").write_bytes(b"x" * 64)
    (vids / "b.mp4").write_bytes(b"y" * 64)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(vids)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        key = win._db.scan_key(vids, False)
        assert pump(qapp, lambda: win._db.load_scan(key) is not None, timeout=30)

        win.sidebar.files_dropped.emit([str(vids / "a.mp4")], str(other))
        qapp.processEvents()

        entries = win._db.load_scan(key)
        assert [Path(e["path"]).name for e in entries] == ["b.mp4"]
    finally:
        win.close()


def test_scan_finish_drops_vanished_tiles(qapp, cache_dir, tmp_path):
    # A file moved away externally while the folder was open: the walk is
    # the truth, and finishing must drop the ghost row — the scan-cache
    # replay would otherwise keep resurrecting it on every reopen.
    folder = _dummy_folder(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win._open_folder(folder)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        (folder / "a.mp4").rename(tmp_path / "a.mp4")  # moved out, silently

        from video_previewer.workers.scanner import ScanResult

        sb = (folder / "b.mp4").stat()
        win._on_scan_finished(
            ScanResult(
                folder=folder,
                recursive=False,
                fresh={(folder / "b.mp4").as_posix(): (sb.st_size, sb.st_mtime)},
            ),
            win._scan_gen,
        )
        assert win.model.count() == 1
        assert win.model.item_at(0).filename == "b.mp4"
    finally:
        win.close()


def test_sidebar_drop_during_drag_is_not_reconciled(qapp, cache_dir, tmp_path,
                                                    monkeypatch):
    # The sidebar drop lands inside the drag loop, then drag_finished fires
    # with MoveAction; the window must NOT also drop the tiles (it already
    # moved/updated them itself).
    root = tmp_path / "root"
    vids = root / "vids"
    other = root / "other"
    vids.mkdir(parents=True)
    other.mkdir()
    (vids / "a.mp4").write_bytes(b"x" * 64)
    (vids / "b.mp4").write_bytes(b"y" * 64)
    fake = _install_fake_drag(monkeypatch)
    fake.result = Qt.DropAction.MoveAction
    win = MainWindow()
    win.show()
    try:
        win._open_folder(vids)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)

        def drop(source) -> None:  # runs "during" the drag, like dropEvent
            win.sidebar.files_dropped.emit([str(vids / "a.mp4")], str(other))

        fake.on_exec = drop
        _press_drag(win.grid, _row_center(win, 0))

        assert (other / "a.mp4").exists()
        assert win.model.count() == 1  # moved out: exactly one removal
        assert win.model.item_at(0).filename == "b.mp4"
    finally:
        win.close()
