"""Rename dialog: edit the stem of one selected video, extension fixed.

The extension never changes (renaming ``a.mp4`` to ``a.mkv`` would break
the file association, not rename it), so the dialog edits only the stem
and validates it: empty, path separators, characters illegal on Windows,
trailing dots/spaces and reserved Windows device names all disable OK.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)

from .. import config

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

#: Characters Windows forbids in file names (kept restricted everywhere so
#: a renamed file stays valid if the library travels between platforms).
_ILLEGAL_CHARS = frozenset('<>:"/\\|?*')

#: Reserved Windows device names, stem-only, case-insensitive.
_RESERVED_STEMS = frozenset(
    {
        "con", "prn", "aux", "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def split_stem(filename: str) -> tuple[str, str]:
    """``('holiday movie', '.mp4')`` for ``'holiday movie.mp4'``."""
    dot = filename.rfind(".")
    if dot <= 0:  # no extension, or a dotfile like '.mp4' (whole name = stem)
        return filename, ""
    return filename[:dot], filename[dot:]


def is_valid_stem(stem: str) -> bool:
    """Whether *stem* can name a file on every supported platform."""
    if not stem or stem != stem.strip(" ."):
        return False
    if any(ch in _ILLEGAL_CHARS or ord(ch) < 32 for ch in stem):
        return False
    return stem.lower() not in _RESERVED_STEMS


def build_rename_dialog(
    parent: "QWidget | None", filename: str
) -> tuple[QDialog, QLineEdit]:
    """Build (but do not run) the rename dialog.

    Returns the dialog and its stem editor so tests can drive validation
    without blocking on a modal loop.
    """
    stem, ext = split_stem(filename)
    dlg = QDialog(parent)
    dlg.setWindowTitle(config.APP_NAME)
    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel(f"Rename (extension {ext} stays unchanged):", dlg))
    edit = QLineEdit(stem, dlg)
    edit.selectAll()
    layout.addWidget(edit)
    buttons = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
        dlg,
    )
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)

    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
    ok.setEnabled(is_valid_stem(edit.text()))
    edit.textChanged.connect(lambda text: ok.setEnabled(is_valid_stem(text)))
    # Enter must respect the same gate as the OK button (returnPressed fires
    # regardless of the button's enabled state).
    edit.returnPressed.connect(
        lambda: dlg.accept() if is_valid_stem(edit.text()) else None
    )
    return dlg, edit


def ask_new_stem(parent: "QWidget | None", filename: str) -> str | None:
    """Run the dialog; the new stem, or None when cancelled.

    OK can only be clicked with a valid stem, but re-validate here too:
    Enter accepts the dialog regardless of the button state.
    """
    dlg, edit = build_rename_dialog(parent, filename)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    stem = edit.text().strip()
    return stem if is_valid_stem(stem) else None