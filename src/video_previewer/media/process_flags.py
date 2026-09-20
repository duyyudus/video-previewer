"""Platform-specific flags for external media tools."""

from __future__ import annotations

import subprocess
import sys


# A windowed Windows application has no parent console for ffmpeg/ffprobe to
# inherit. Without this flag, each job briefly creates its own visible console.
WINDOWLESS_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
)
