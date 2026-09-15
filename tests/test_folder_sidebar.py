"""Folder sidebar: directory-only tree, click semantics, toggle persistence."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QModelIndex

from conftest import pump
from video_previewer import config
from video_previewer.ui.folder_sidebar import FolderSidebar
from video_previewer.ui.main_window import MainWindow


def _tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """root/ with alpha (2 videos + a sub folder), beta (empty), a text file."""
    root = tmp_path / "tree"
    alpha = root / "alpha"
    beta = root / "beta"
    alpha.mkdir(parents=True)
    beta.mkdir()
    (root / "note.txt").write_text("not a folder")
    (alpha / "a.mp4").write_bytes(b"x" * 64)
    (alpha / "b.mp4").write_bytes(b"y" * 64)
    (alpha / "sub").mkdir()
    (alpha / "sub" / "c.mp4").write_bytes(b"z" * 64)
    return root, alpha, beta


def _index(sidebar: FolderSidebar, path: Path) -> QModelIndex:
    return sidebar._fs_model.index(str(path))


def test_sidebar_lists_directories_only(qapp, tmp_path):
    root, alpha, beta = _tree(tmp_path)
    sidebar = FolderSidebar()
    sidebar.set_root(root)
    model = sidebar._fs_model
    try:
        assert pump(qapp, lambda: model.rowCount(model.index(str(root))) == 2)
        names = sorted(
            model.index(row, 0, model.index(str(root))).data() for row in range(2)
        )
        assert names == ["alpha", "beta"]  # note.txt is not a folder
    finally:
        sidebar.deleteLater()
        qapp.processEvents()


def test_double_click_scans_folder(qapp, cache_dir, tmp_path):
    root, alpha, beta = _tree(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win.sidebar.set_root(root)
        assert pump(qapp, lambda: _index(win.sidebar, alpha).isValid(), timeout=30)
        win.sidebar.doubleClicked.emit(_index(win.sidebar, alpha))
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        assert win._folder_label.text() == str(alpha)
        assert win._settings.value(config.SETTING_LAST_FOLDER) == str(alpha)
        # opening a folder always ends with its children revealed
        assert win.sidebar.isExpanded(_index(win.sidebar, alpha))
    finally:
        win.close()


def test_single_click_toggles_but_never_scans(qapp, cache_dir, tmp_path):
    root, alpha, beta = _tree(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win.sidebar.set_root(root)
        assert pump(qapp, lambda: _index(win.sidebar, alpha).isValid(), timeout=30)
        index = _index(win.sidebar, alpha)

        win.sidebar.clicked.emit(index)
        assert win.model.count() == 0  # no scan started
        assert win._folder_label.text() == "No folder selected"
        assert win.sidebar.isExpanded(index)  # first click expands

        win.sidebar.clicked.emit(index)
        assert not win.sidebar.isExpanded(index)  # second click collapses

        # A double-click ends expanded even though its first click just
        # collapsed the folder again.
        win.sidebar.doubleClicked.emit(index)
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
        assert win.sidebar.isExpanded(index)
    finally:
        win.close()


def test_double_click_honors_subfolders_toggle(qapp, cache_dir, tmp_path):
    root, alpha, beta = _tree(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win.sidebar.set_root(root)
        assert pump(qapp, lambda: _index(win.sidebar, alpha).isValid(), timeout=30)
        index = _index(win.sidebar, alpha)

        win.sidebar.doubleClicked.emit(index)  # Subfolders unchecked: flat
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)

        win._recursive_chk.setChecked(True)  # rescans alpha recursively
        assert pump(qapp, lambda: win.model.count() == 3, timeout=30)
    finally:
        win.close()


def test_open_folder_reveals_selection_in_sidebar(qapp, cache_dir, tmp_path,
                                                  monkeypatch):
    # Opening a folder through the picker (or the startup restore) selects it
    # in the tree, so both views stay in sync.
    root, alpha, beta = _tree(tmp_path)
    monkeypatch.setattr(
        "video_previewer.ui.main_window.QFileDialog.getExistingDirectory",
        lambda *args, **kwargs: str(alpha),
    )
    win = MainWindow()
    win.show()
    try:
        win.sidebar.set_root(root)
        win._browse()  # the stubbed dialog returns alpha
        assert pump(
            qapp,
            lambda: win.sidebar.currentIndex().data() == alpha.name,
            timeout=30,
        )
        assert pump(qapp, lambda: win.model.count() == 2, timeout=30)
    finally:
        win.close()


def test_toggle_hides_sidebar_and_persists(qapp, cache_dir, tmp_path,
                                           file_settings):
    win = MainWindow()
    win.show()
    win.resize(700, 500)  # fits the offscreen screen (see geometry tests)
    try:
        assert win.sidebar.isVisible()  # visible by default
        qapp.processEvents()
        win._splitter.setSizes([400, 1])
        qapp.processEvents()
        assert 300 < win._splitter.sizes()[0] < 500

        win._sidebar_btn.setChecked(False)
        assert not win.sidebar.isVisible()
    finally:
        win.close()
    win.deleteLater()

    win2 = MainWindow()
    win2.show()
    try:
        assert win2._sidebar_btn.isChecked() is False
        assert not win2.sidebar.isVisible()  # hidden state survived
        win2._sidebar_btn.setChecked(True)  # back on: remembered width returns
        qapp.processEvents()
        assert 300 < win2._splitter.sizes()[0] < 500  # split width survived
    finally:
        win2.close()


def test_double_click_empty_folder_fails_soft(qapp, cache_dir, tmp_path):
    root, alpha, beta = _tree(tmp_path)
    win = MainWindow()
    win.show()
    try:
        win.sidebar.set_root(root)
        assert pump(qapp, lambda: _index(win.sidebar, beta).isValid(), timeout=30)
        win.sidebar.doubleClicked.emit(_index(win.sidebar, beta))
        assert pump(qapp, lambda: win._folder_label.text() == str(beta), timeout=30)
        assert pump(qapp, lambda: win._status_label.text() == "0 videos", timeout=30)
        assert win.model.count() == 0
    finally:
        win.close()