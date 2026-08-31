"""Application constants, paths and small configuration helpers.

Tunables are loaded from ``settings.yml`` in the project root (override the
location with the ``VIDEO_PREVIEWER_SETTINGS`` environment variable). A
missing file, or a missing/invalid entry, falls back to the built-in
defaults below, so a bad edit can never break the app.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from shutil import which
from typing import Any

import yaml

log = logging.getLogger(__name__)

APP_NAME = "Video Previewer"
ORG_NAME = "video-previewer"

# Folder-scanning settings (persisted via QSettings)
SETTING_LAST_FOLDER = "last_folder"
SETTING_RECURSIVE = "recursive"

# --- settings.yml -------------------------------------------------------------

#: Built-in defaults; entries in settings.yml override these, one by one.
DEFAULTS: dict[str, Any] = {
    # Supported video file extensions (lowercase, dot included).
    "supported_extensions": [".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"],
    # Thumbnails: width (px); extraction position as a fraction of the
    # duration (frame zero is often a black frame / title card); fallback
    # position (s) for unknown/short videos; per-invocation timeouts (s)
    # for ffmpeg/ffprobe; max parallel thumbnail jobs.
    "thumb_width": 320,
    "thumb_position_ratio": 0.15,
    "thumb_fallback_seconds": 1.0,
    "thumb_extract_timeout": 60,
    "probe_timeout": 10,
    "thumb_concurrency": 4,
    # Scanner: items emitted per batch.
    "scan_batch": 64,
    # Grid layout: preferred tile width (px); tile aspect (w/h); filename
    # row height (px); tile spacing (px); column-count bounds.
    "cell_width": 220,
    "cell_aspect": 9 / 16,
    "filename_row": 24,
    "grid_spacing": 8,
    "min_cols": 2,
    "max_cols": 10,
    # Hover preview playback: grace period before autoplay (ms); minimum
    # gap between seeks (ms, ~30 seeks/second maximum).
    "autoplay_delay_ms": 200,
    "seek_throttle_ms": 33,
}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_FILE = _PROJECT_ROOT / "settings.yml"

_settings_data: dict[str, Any] = {}

# Tunables, bound from settings.yml by load_settings() (called at import).
SUPPORTED_EXTENSIONS: frozenset[str]
THUMB_WIDTH: int
THUMB_POSITION_RATIO: float
THUMB_FALLBACK_SECONDS: float
THUMB_EXTRACT_TIMEOUT: int
PROBE_TIMEOUT: int
THUMB_CONCURRENCY: int
SCAN_BATCH: int
CELL_WIDTH: int
CELL_ASPECT: float
FILENAME_ROW: int
GRID_SPACING: int
MIN_COLS: int
MAX_COLS: int
AUTOPLAY_DELAY_MS: int
SEEK_THROTTLE_MS: int


def settings_path() -> Path:
    """Location of the settings file (``VIDEO_PREVIEWER_SETTINGS`` wins)."""
    override = os.environ.get("VIDEO_PREVIEWER_SETTINGS")
    if override:
        return Path(override).expanduser()
    return _SETTINGS_FILE


def _read_settings_file() -> dict[str, Any]:
    """Parse the settings file; never raises (fail soft, defaults win)."""
    path = settings_path()
    if not path.is_file():
        if os.environ.get("VIDEO_PREVIEWER_SETTINGS"):
            log.warning(
                "settings file not found: %s — using built-in defaults", path
            )
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        log.warning("could not read %s (%s) — using built-in defaults", path, exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        log.warning("%s must be a YAML mapping — using built-in defaults", path)
        return {}
    return data


def _num(key: str, cast: type) -> int | float:
    """Value of *key* as *cast* (int/float), falling back to its default."""
    default = DEFAULTS[key]
    value = _settings_data.get(key, default)
    if value is None or isinstance(value, bool):
        log.warning("settings: %s must be a number, using default %s", key, default)
        return cast(default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        log.warning(
            "settings: %s is not a valid number, using default %s", key, default
        )
        return cast(default)


def _extensions() -> frozenset[str]:
    """supported_extensions as dot-prefixed lowercase entries.

    User entries are normalised (``"MP4"`` -> ``".mp4"``); an invalid entry
    falls back to the defaults.
    """
    default = frozenset(str(e).lower() for e in DEFAULTS["supported_extensions"])
    value = _settings_data.get("supported_extensions")
    if value is None:
        return default
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(e, str) for e in value
    ):
        log.warning(
            "settings: supported_extensions must be a list of extensions, "
            "using defaults"
        )
        return default
    result = set()
    for e in value:
        e = e.strip().lower()
        if e and not e.startswith("."):
            e = "." + e
        if e:
            result.add(e)
    return frozenset(result) if result else default


def load_settings() -> None:
    """(Re)read settings.yml and (re)bind every tunable constant.

    Called once at import time; call again after changing
    ``VIDEO_PREVIEWER_SETTINGS`` or the file contents (e.g. in tests).
    """
    global SUPPORTED_EXTENSIONS, THUMB_WIDTH, THUMB_POSITION_RATIO
    global THUMB_FALLBACK_SECONDS, THUMB_EXTRACT_TIMEOUT, PROBE_TIMEOUT
    global THUMB_CONCURRENCY, SCAN_BATCH, CELL_WIDTH, CELL_ASPECT
    global FILENAME_ROW, GRID_SPACING, MIN_COLS, MAX_COLS
    global AUTOPLAY_DELAY_MS, SEEK_THROTTLE_MS

    _settings_data.clear()
    _settings_data.update(_read_settings_file())

    SUPPORTED_EXTENSIONS = _extensions()
    THUMB_WIDTH = _num("thumb_width", int)
    THUMB_POSITION_RATIO = _num("thumb_position_ratio", float)
    THUMB_FALLBACK_SECONDS = _num("thumb_fallback_seconds", float)
    THUMB_EXTRACT_TIMEOUT = _num("thumb_extract_timeout", int)
    PROBE_TIMEOUT = _num("probe_timeout", int)
    THUMB_CONCURRENCY = _num("thumb_concurrency", int)
    SCAN_BATCH = _num("scan_batch", int)
    CELL_WIDTH = _num("cell_width", int)
    CELL_ASPECT = _num("cell_aspect", float)
    FILENAME_ROW = _num("filename_row", int)
    GRID_SPACING = _num("grid_spacing", int)
    MIN_COLS = _num("min_cols", int)
    MAX_COLS = _num("max_cols", int)
    AUTOPLAY_DELAY_MS = _num("autoplay_delay_ms", int)
    SEEK_THROTTLE_MS = _num("seek_throttle_ms", int)


load_settings()

# --- Media binaries ---------------------------------------------------------


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
