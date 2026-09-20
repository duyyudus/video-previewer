"""Application icon tests."""

from __future__ import annotations

from PySide6.QtGui import QIcon

from video_previewer import app as app_module


def test_app_icon_asset_is_loadable(qapp) -> None:
    path = app_module._asset_path("video-previewer.png")

    assert path.is_file()
    assert not QIcon(str(path)).isNull()

    app_module._apply_app_icon(qapp)
    assert not qapp.windowIcon().isNull()
