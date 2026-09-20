"""Application bootstrap: QApplication setup + main window."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication

from . import config
from .ui.main_window import MainWindow


def _asset_path(name: str) -> Path:
    """Return an asset path in a checkout or a PyInstaller bundle."""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir is not None:
        return Path(bundle_dir) / "assets" / name
    return Path(__file__).resolve().parents[2] / "assets" / name


def _set_windows_app_id() -> None:
    """Give Windows a stable identity for taskbar grouping and icons."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "video-previewer.desktop"
        )
    except (AttributeError, OSError):
        pass


def _apply_app_icon(app: QApplication) -> None:
    icon = QIcon(str(_asset_path("video-previewer.png")))
    if not icon.isNull():
        app.setWindowIcon(icon)


def _apply_dark_palette(app: QApplication) -> None:
    app.setStyle("Fusion")
    p = QPalette()
    p.setColor(QPalette.ColorRole.Window, QColor(24, 24, 28))
    p.setColor(QPalette.ColorRole.WindowText, QColor(230, 230, 236))
    p.setColor(QPalette.ColorRole.Base, QColor(18, 18, 22))
    p.setColor(QPalette.ColorRole.AlternateBase, QColor(32, 32, 38))
    p.setColor(QPalette.ColorRole.ToolTipBase, QColor(230, 230, 236))
    p.setColor(QPalette.ColorRole.ToolTipText, QColor(230, 230, 236))
    p.setColor(QPalette.ColorRole.Text, QColor(230, 230, 236))
    p.setColor(QPalette.ColorRole.Button, QColor(44, 44, 52))
    p.setColor(QPalette.ColorRole.ButtonText, QColor(230, 230, 236))
    p.setColor(QPalette.ColorRole.BrightText, QColor(255, 90, 90))
    p.setColor(QPalette.ColorRole.Link, QColor(90, 160, 230))
    p.setColor(QPalette.ColorRole.Highlight, QColor(42, 100, 180))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 140, 150))
    disabled_text = QColor(130, 130, 140)
    for group in (QPalette.ColorGroup.Disabled,):
        p.setColor(group, QPalette.ColorRole.Text, disabled_text)
        p.setColor(group, QPalette.ColorRole.ButtonText, disabled_text)
        p.setColor(group, QPalette.ColorRole.WindowText, disabled_text)
    app.setPalette(p)


def run() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("video-previewer")
    app.setOrganizationName(config.ORG_NAME)
    _apply_app_icon(app)
    _apply_dark_palette(app)
    window = MainWindow()
    window.show()
    return app.exec()
