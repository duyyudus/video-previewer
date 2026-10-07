"""Dialogs for the rotate, change-ratio, convert, and extract-frames
actions: overwrite / format + sampling prompts + modal progress."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from .. import config
from ..media.aspect import AspectRatio
from ..media.frames import FOLDER_SUFFIX, SAMPLE_PRESETS, FrameFormat, FrameOptions
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


_FORMAT_HINTS = {
    FrameFormat.PNG: "lossless, slowest",
    FrameFormat.PNG_FAST: "lossless, larger files, somewhat faster",
    FrameFormat.JPEG: "fastest and much smaller, slightly lossy",
}


class FrameOptionsDialog(QDialog):
    """Image format + frames-per-second sampling for an extraction run.

    Sampling is picked per frame-rate family (the selection may mix them);
    the last entry of each list keeps every frame. Starts on the
    :class:`FrameOptions` defaults.
    """

    def __init__(
        self,
        parent: QWidget | None,
        filenames: Sequence[str],
        root: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Extract frames")
        self.setMinimumWidth(420)
        defaults = FrameOptions()
        count = len(filenames)
        what = f'"{filenames[0]}"' if count == 1 else f"{count} selected videos"
        heading = QLabel(f"Extract frames of {what}:", self)
        heading.setWordWrap(True)

        self._format = QComboBox(self)
        self._format.setObjectName("framesFormat")
        for fmt in FrameFormat:
            self._format.addItem(f"{fmt.label} — {_FORMAT_HINTS[fmt]}", fmt)
        self._format.setCurrentIndex(self._format.findData(defaults.fmt))

        form = QFormLayout()
        form.addRow("Image format:", self._format)
        self._rates: dict[int, QComboBox] = {}
        default_rates = {24: defaults.rate_24, 30: defaults.rate_30}
        for family, rates in SAMPLE_PRESETS.items():
            combo = QComboBox(self)
            combo.setObjectName(f"framesRate{family}")
            for rate in rates:
                text = (
                    f"{rate} frames per second (all frames)" if rate == family
                    else f"{rate} frames per second"
                )
                combo.addItem(text, rate)
            combo.setCurrentIndex(max(0, combo.findData(default_rates[family])))
            self._rates[family] = combo
            form.addRow(f"{family} fps videos:", combo)

        where = f"in {root}" if root is not None else "next to each video"
        note = QLabel(
            "Other frame rates use the nearer row (25 fps as 24, 60 fps as "
            f"30).\nThe images go into a <video name>{FOLDER_SUFFIX} folder "
            f"{where}.",
            self,
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Extract")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def options(self) -> FrameOptions:
        return FrameOptions(
            fmt=self._format.currentData(),
            rate_24=self._rates[24].currentData(),
            rate_30=self._rates[30].currentData(),
        )


def ask_frame_options(
    parent: QWidget, filenames: Sequence[str], root: Path | None = None
) -> FrameOptions | None:
    """Format + sampling for the extraction; None: cancel it.

    *root* is the custom output folder (None = next to each video)."""
    dialog = FrameOptionsDialog(parent, filenames, root)
    try:
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.options()
    finally:
        dialog.deleteLater()


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
