"""Application bootstrap: QApplication setup + main window."""

from __future__ import annotations

import sys

from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import QLoggingCategory, Qt
from PySide6.QtWidgets import QApplication

from . import config
from .ui.main_window import MainWindow


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
    app = QApplication(sys.argv)
    app.setApplicationName("video-previewer")
    app.setOrganizationName(config.ORG_NAME)
    # The Qt FFmpeg backend logs every opened file at info level; keep only
    # warnings so the console stays quiet while previews are played.
    QLoggingCategory.setFilterRules("qt.multimedia.ffmpeg.warning=true")
    _apply_dark_palette(app)
    window = MainWindow()
    window.show()
    return app.exec()
