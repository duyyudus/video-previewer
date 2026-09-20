"""Confirmation dialog for irreversible video deletion."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtWidgets import QMessageBox, QWidget

from .. import config


def confirm_permanent_delete(parent: QWidget, filenames: Sequence[str]) -> bool:
    """Ask whether *filenames* should be permanently deleted."""
    count = len(filenames)
    if count == 1:
        question = f'Permanently delete "{filenames[0]}"?'
    else:
        question = f"Permanently delete {count} selected videos?"

    box = QMessageBox(
        QMessageBox.Icon.Warning,
        config.APP_NAME,
        question,
        parent=parent,
    )
    box.setInformativeText(
        "The selected video will not be moved to Trash. This cannot be undone."
        if count == 1
        else "The selected videos will not be moved to Trash. "
        "This cannot be undone."
    )
    delete_button = box.addButton(
        "Delete Permanently", QMessageBox.ButtonRole.DestructiveRole
    )
    cancel_button = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel_button)
    box.setEscapeButton(cancel_button)
    if count > 1:
        box.setDetailedText("\n".join(filenames))
    box.exec()
    return box.clickedButton() is delete_button
