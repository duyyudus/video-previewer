"""PyInstaller entry point for the Windows GUI bundle."""

from __future__ import annotations

import os
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    os.environ.setdefault(
        "VIDEO_PREVIEWER_SETTINGS",
        str(Path(sys.executable).with_name("settings.yml")),
    )

from video_previewer.main import main  # noqa: E402


if __name__ == "__main__":
    main()
