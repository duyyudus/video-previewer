"""Dialogs for the rotate, change-ratio, and convert actions: overwrite
prompts + modal progress."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..media.aspect import AspectRatio
from ..media.rotator import BACKUP_DIR_NAME, RotateDirection


def ask_overwrite(
    parent: QWidget, filenames: Sequence[str], direction: RotateDirection
) -> bool | None:
    """True: overwrite the originals; False: keep them in ``.vpbackup/``;
    None: cancel the rotation."""
    count = len(filenames)
    what = f'"{filenames[0]}"' if count == 1 else f"{count} selected videos"
    box = QMessageBox(
        QMessageBox.Icon.Question,
        config.APP_NAME,
        f"Rotate {what} {direction.label.lower()} and overwrite the original"
        f"{' file' if count == 1 else 's'}?",
        parent=parent,
    )
    box.setInformativeText(
        f"Yes replaces the original. No keeps the original in a "
        f"{BACKUP_DIR_NAME} folder next to it."
    )
    return _ask_yes_no_cancel(box, filenames)


def ask_aspect_overwrite(
    parent: QWidget, filenames: Sequence[str], ratio: AspectRatio
) -> bool | None:
    """True: overwrite the originals; False: keep them in ``.vpbackup/``;
    None: cancel the ratio change."""
    count = len(filenames)
    what = f'"{filenames[0]}"' if count == 1 else f"{count} selected videos"
    box = QMessageBox(
        QMessageBox.Icon.Question,
        config.APP_NAME,
        f"Stretch {what} to {ratio.label} and overwrite the original"
        f"{' file' if count == 1 else 's'}?",
        parent=parent,
    )
    box.setInformativeText(
        f"The height is kept and the width is stretched or squashed to fit. "
        f"Yes replaces the original. No keeps the original in a "
        f"{BACKUP_DIR_NAME} folder next to it."
    )
    return _ask_yes_no_cancel(box, filenames)


def ask_convert_overwrite(
    parent: QWidget, filenames: Sequence[str], skipped: int = 0
) -> bool | None:
    """True: delete the originals after converting; False: keep them in
    ``.vpbackup/``; None: cancel the conversion. *skipped* counts selected
    videos that already are MP4 and will be left alone."""
    count = len(filenames)
    what = f'"{filenames[0]}"' if count == 1 else f"{count} videos"
    box = QMessageBox(
        QMessageBox.Icon.Question,
        config.APP_NAME,
        f"Convert {what} to MP4 and overwrite the original"
        f"{' file' if count == 1 else 's'}?",
        parent=parent,
    )
    info = (
        f"Yes deletes the original. No keeps the original in a "
        f"{BACKUP_DIR_NAME} folder next to it."
    )
    if skipped:
        info += (
            f"\n\n{skipped} selected video{' is' if skipped == 1 else 's are'} "
            "already MP4 and will be skipped."
        )
    box.setInformativeText(info)
    return _ask_yes_no_cancel(box, filenames)


def _ask_yes_no_cancel(box: QMessageBox, filenames: Sequence[str]) -> bool | None:
    yes = box.addButton(QMessageBox.StandardButton.Yes)
    no = box.addButton(QMessageBox.StandardButton.No)
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(no)
    box.setEscapeButton(cancel)
    if len(filenames) > 1:
        box.setDetailedText("\n".join(filenames))
    box.exec()
    clicked = box.clickedButton()
    if clicked is yes:
        return True
    if clicked is no:
        return False
    return None


class RotateProgressDialog(QDialog):
    """Application-modal progress while videos are rotated, reshaped, or converted.

    The whole app is locked until the job reports back. Cancel, Esc, or
    closing the window only *request* cancellation: the dialog stays up
    (still modal) until :meth:`finish`, so nothing can touch the files while
    the worker winds down.
    """

    cancel_requested = Signal()

    def __init__(
        self, parent: QWidget | None = None, title: str = "Rotating videos"
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(460)
        self._label = QLabel("Starting…", self)
        self._label.setWordWrap(True)
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(True)
        self._bar.setFormat("%p%")
        self._buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, self)
        self._buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        layout.addWidget(self._bar)
        layout.addWidget(self._buttons)
        self._cancelling = False
        self._finished = False

    def set_progress(self, permille: int, text: str) -> None:
        if self._cancelling:
            return
        self._bar.setValue(permille)
        if text:
            self._label.setText(text)

    @property
    def cancelling(self) -> bool:
        return self._cancelling

    def reject(self) -> None:
        # Cancel button and Esc land here.
        self._request_cancel()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._finished:
            super().closeEvent(event)
            return
        event.ignore()
        self._request_cancel()

    def finish(self) -> None:
        """The job is over: release the app."""
        self._finished = True
        self.done(QDialog.DialogCode.Accepted)

    def _request_cancel(self) -> None:
        if self._finished or self._cancelling:
            return
        self._cancelling = True
        self._label.setText("Cancelling…")
        self._buttons.setEnabled(False)
        self.cancel_requested.emit()
