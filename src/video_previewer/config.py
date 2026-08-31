"""Application constants, paths and small configuration helpers."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from shutil import which

APP_NAME = "Video Previewer"
ORG_NAME = "video-previewer"

# Folder-scanning settings (persisted via QSettings)
SETTING_LAST_FOLDER = "last_folder"
SETTING_RECURSIVE = "recursive"

# Supported video file extensions (lowercase, dot included).
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
)

# --- Thumbnail generation -------------------------------------------------
# Thumbnails are extracted at ~15% into the video (frame zero is often a
# black frame / title card).
THUMB_WIDTH = 320
THUMB_POSITION_RATIO = 0.15
THUMB_FALLBACK_SECONDS = 1.0
THUMB_EXTRACT_TIMEOUT = 60  # seconds, per ffmpeg invocation
PROBE_TIMEOUT = 10  # seconds, per ffprobe invocation

# --- Concurrency ----------------------------------------------------------
THUMB_CONCURRENCY = 4  # max parallel thumbnail jobs
SCAN_BATCH = 64  # items emitted per scanner batch

# --- Grid layout ----------------------------------------------------------
CELL_WIDTH = 220  # preferred thumbnail width in px
CELL_ASPECT = 9 / 16
FILENAME_ROW = 24  # px reserved for the filename below the thumbnail
GRID_SPACING = 8
MIN_COLS = 2
MAX_COLS = 10

# --- Hover preview playback -----------------------------------------------
AUTOPLAY_DELAY_MS = 200  # grace period before a hover starts playback
SEEK_THROTTLE_MS = 33  # ~30 seeks/second maximum

# --- Media binaries -------------------------------------------------------


def ffmpeg_path() -> str | None:
    """Locate the ffmpeg binary (override with VIDEO_PREVIEWER_FFMPEG)."""
    override = os.environ.get("VIDEO_PREVIEWER_FFMPEG")
    return override or which("ffmpeg")


def ffprobe_path() -> str | None:
    """Locate the ffprobe binary (override with VIDEO_PREVIEWER_FFPROBE)."""
    override = os.environ.get("VIDEO_PREVIEWER_FFPROBE")
    return override or which("ffprobe")


def app_cache_dir() -> Path:
    """Per-user cache directory for thumbnails + metadata.

    Override with VIDEO_PREVIEWER_CACHE_DIR (used by tests).
    """
    override = os.environ.get("VIDEO_PREVIEWER_CACHE_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / ORG_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / ORG_NAME
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / ORG_NAME


def thumbnail_dir() -> Path:
    return app_cache_dir() / "thumbnails"


def database_path() -> Path:
    return app_cache_dir() / "metadata.sqlite"
