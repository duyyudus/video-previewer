"""Exit prompt: keep or discard the selected folder and its videos.

Shown when the window closes with a folder open. The "remember" checkbox is
off by default: unless the user opts in, the folder and its cached videos
(thumbnails, metadata, scan results) are discarded.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)

from .. import config

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget


def build_dialog(
    parent: QWidget | None, folder: Path, video_count: int
) -> tuple[QDialog, QCheckBox]:
    """Build (but do not run) the keep/discard dialog.

    Returns the dialog and its "remember" checkbox so tests can inspect the
    default state without blocking on a modal loop.
    """
    word = "video" if video_count == 1 else "videos"
    dlg = QDialog(parent)
    dlg.setWindowTitle(config.APP_NAME)
    layout = QVBoxLayout(dlg)
    layout.addWidget(
        QLabel(f"Keep {folder} and its {video_count} {word} for next time?")
    )
    remember = QCheckBox("Remember this folder and its videos", dlg)
    # Off by default: the folder is not remembered unless the user checks it.
    layout.addWidget(remember)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    return dlg, remember


def ask_keep_on_exit(parent: QWidget | None, folder: Path, video_count: int) -> bool:
    """Run the dialog; True if the user wants to keep the folder + videos."""
    dlg, remember = build_dialog(parent, folder, video_count)
    dlg.exec()
    return remember.isChecked()
