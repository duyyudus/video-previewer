"""Cross-platform file deletion primitives."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFile


def move_to_trash(path: Path) -> bool:
    """Move *path* to the platform trash/recycle bin.

    PySide exposes Qt's output parameter as a ``(success, path_in_trash)``
    tuple on some versions and as a plain bool on others. A path that has
    already disappeared is considered successfully reconciled.
    """
    if not path.exists():
        return True
    result = QFile.moveToTrash(str(path))
    if isinstance(result, tuple):
        return bool(result[0])
    return bool(result)
