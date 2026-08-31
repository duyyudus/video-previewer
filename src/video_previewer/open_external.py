"""Open a file with the OS-default application (the user's video player).

Fail soft: any error is logged and reported as ``False``, never raised —
a missing/broken association must not crash the app.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def open_externally(path: str | Path) -> bool:
    """Open *path* with the OS-default application. Returns True on success."""
    p = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # noqa: S606 - intentional OS default association
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return True
    except Exception:
        log.exception("failed to open %s with the OS default player", p)
        return False
