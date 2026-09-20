"""Independent exit choices for folder restoration and cached video data."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class ExitChoices:
    """Preferences selected when closing with a folder open."""

    remember_folder: bool
    keep_cache: bool


def build_dialog(
    parent: QWidget | None, folder: Path, video_count: int
) -> tuple[QDialog, QCheckBox, QCheckBox]:
    """Build (but do not run) the exit-options dialog.

    Folder restoration is off by default, while the expensive thumbnail
    cache is retained. They are separate controls so a user can start with
    an empty window without paying to regenerate thumbnails later.
    """
    word = "video" if video_count == 1 else "videos"
    dlg = QDialog(parent)
    dlg.setWindowTitle(config.APP_NAME)
    layout = QVBoxLayout(dlg)
    layout.addWidget(
        QLabel(f"Closing {folder} ({video_count} {word}). What should be kept?")
    )
    remember_folder = QCheckBox("Reopen this folder next time", dlg)
    layout.addWidget(remember_folder)
    keep_cache = QCheckBox("Keep cached thumbnails and metadata", dlg)
    keep_cache.setChecked(True)
    layout.addWidget(keep_cache)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok, dlg)
    buttons.accepted.connect(dlg.accept)
    layout.addWidget(buttons)
    return dlg, remember_folder, keep_cache


def ask_exit_choices(
    parent: QWidget | None, folder: Path, video_count: int
) -> ExitChoices:
    """Run the dialog and return both independent persistence choices."""
    dlg, remember_folder, keep_cache = build_dialog(parent, folder, video_count)
    dlg.exec()
    return ExitChoices(
        remember_folder=remember_folder.isChecked(),
        keep_cache=keep_cache.isChecked(),
    )
