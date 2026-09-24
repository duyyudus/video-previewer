"""Application constants, paths and small configuration helpers.

Tunables are loaded from ``settings.yml`` in the project root (override the
location with the ``VIDEO_PREVIEWER_SETTINGS`` environment variable). A
missing file, or a missing/invalid entry, falls back to the built-in
defaults below, so a bad edit can never break the app. Numeric entries are
additionally clamped to their valid range: an out-of-range value (e.g.
``thumb_concurrency: 0``, which would starve the thumbnail queue) is logged
and pinned to the nearest bound instead of being used as-is. A null
``thumb_concurrency`` automatically uses the machine's logical CPU count.
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

# Settings persisted via QSettings
SETTING_LAST_FOLDER = "last_folder"
SETTING_RECURSIVE = "recursive"
SETTING_WINDOW_GEOMETRY = "window_geometry"
SETTING_SIDEBAR_VISIBLE = "sidebar_visible"
SETTING_SIDEBAR_SPLITTER = "sidebar_splitter_state"
SETTING_SORT_KEY = "sort_key"
SETTING_SORT_ORDER = "sort_order"

# --- settings.yml -------------------------------------------------------------

#: Built-in defaults; entries in settings.yml override these, one by one.
DEFAULTS: dict[str, Any] = {
    # Persistent thumbnails, metadata, scan results, and diagnostic logs.
    # None selects the platform-specific per-user cache directory.
    "cache_dir": None,
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
    # None uses all logical CPUs reported by the operating system.
    "thumb_concurrency": None,
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
    # Compact label shown beside the pointer while dragging videos.  Keeping
    # this much smaller than a tile leaves sidebar drop targets visible.
    "drag_preview_width": 160,
    "drag_preview_height": 28,
    # Hover preview playback: grace period before autoplay (ms); minimum
    # gap between seeks (ms, ~30 seeks/second maximum).
    "autoplay_delay_ms": 200,
    "seek_throttle_ms": 33,
    # Double-click-to-open: maximum pointer drift between the two presses
    # (px, Manhattan length). The timing window comes from the OS.
    "double_click_max_dist": 10,
    # Main window: fallback size (px) when no remembered geometry exists.
    "default_window_width": 1280,
    "default_window_height": 800,
    # Toolbar search field width (px).
    "search_box_width": 310,
    # Pause after typing before applying the search filter (ms).
    "search_debounce_ms": 150,
    # Folder sidebar: initial width (px) before any remembered splitter state.
    "sidebar_width": 260,
    # Video rotation: use NVIDIA NVENC/CUDA when the GPU and ffmpeg support
    # it (falls back to the CPU encoder automatically).
    "rotate_use_cuda": True,
    # Change ratio (stretch/squash): same, for its re-encode.
    "aspect_use_cuda": True,
    # Convert to MP4: same, for the video re-encode (remuxes need no GPU).
    "convert_use_cuda": True,
}

# Repo-checkout assumption: config.py lives at <root>/src/video_previewer/,
# so parents[2] is the project root holding settings.yml. An installed wheel
# (console script) has no settings.yml next to site-packages and silently
# runs on the built-in defaults below; point VIDEO_PREVIEWER_SETTINGS at a
# file to configure an installed copy.
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
DRAG_PREVIEW_WIDTH: int
DRAG_PREVIEW_HEIGHT: int
AUTOPLAY_DELAY_MS: int
SEEK_THROTTLE_MS: int
DOUBLE_CLICK_MAX_DIST: int
DEFAULT_WINDOW_WIDTH: int
DEFAULT_WINDOW_HEIGHT: int
SEARCH_BOX_WIDTH: int
SEARCH_DEBOUNCE_MS: int
SIDEBAR_WIDTH: int
ROTATE_USE_CUDA: bool
ASPECT_USE_CUDA: bool
CONVERT_USE_CUDA: bool
CACHE_DIR: Path | None


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


def _num(
    key: str,
    cast: type,
    minimum: int | float | None = None,
    maximum: int | float | None = None,
) -> int | float:
    """Value of *key* as *cast* (int/float), falling back to its default.

    Out-of-range values are clamped to the nearest bound (with a warning)
    rather than used as-is: ``thumb_concurrency: 0`` would otherwise start
    no thumbnail job ever, and ``scan_batch: 0`` raises ``ValueError``
    inside the scanner's ``range(0, n, 0)`` batching.
    """
    default = DEFAULTS[key]
    value = _settings_data.get(key, default)
    if value is None or isinstance(value, bool):
        log.warning("settings: %s must be a number, using default %s", key, default)
        result = cast(default)
    else:
        try:
            result = cast(value)
        except (TypeError, ValueError):
            log.warning(
                "settings: %s is not a valid number, using default %s", key, default
            )
            result = cast(default)
    if minimum is not None and result < minimum:
        log.warning(
            "settings: %s=%s is below the minimum %s, clamped", key, result, minimum
        )
        result = cast(minimum)
    if maximum is not None and result > maximum:
        log.warning(
            "settings: %s=%s is above the maximum %s, clamped", key, result, maximum
        )
        result = cast(maximum)
    return result


def _bool(key: str) -> bool:
    """Value of *key* as a bool, falling back to its default."""
    default = bool(DEFAULTS[key])
    value = _settings_data.get(key, default)
    if not isinstance(value, bool):
        log.warning("settings: %s must be true or false, using default %s", key, default)
        return default
    return value


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


def _thumb_concurrency() -> int:
    """Configured parallel thumbnail jobs, or the logical CPU count for null."""
    automatic = max(1, os.cpu_count() or 1)
    value = _settings_data.get("thumb_concurrency", DEFAULTS["thumb_concurrency"])
    if value is None:
        return automatic
    if isinstance(value, bool):
        log.warning(
            "settings: thumb_concurrency must be a positive integer or null, "
            "using logical CPU count %s",
            automatic,
        )
        return automatic
    try:
        result = int(value)
    except (TypeError, ValueError):
        log.warning(
            "settings: thumb_concurrency is not a valid number, "
            "using logical CPU count %s",
            automatic,
        )
        return automatic
    if result < 1:
        log.warning(
            "settings: thumb_concurrency=%s is below the minimum 1, clamped",
            result,
        )
        return 1
    return result


def _cache_dir() -> Path | None:
    """Configured cache path, or None for the platform default.

    Relative paths are anchored beside the active settings file so their
    meaning does not depend on the process working directory.
    """
    value = _settings_data.get("cache_dir", DEFAULTS["cache_dir"])
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        log.warning("settings: cache_dir must be a path string, using default")
        return None
    path = Path(os.path.expandvars(value)).expanduser()
    if not path.is_absolute():
        path = settings_path().parent / path
    return path.resolve(strict=False)


def load_settings() -> None:
    """(Re)read settings.yml and (re)bind every tunable constant.

    Called once at import time; call again after changing
    ``VIDEO_PREVIEWER_SETTINGS`` or the file contents (e.g. in tests).
    """
    global SUPPORTED_EXTENSIONS, THUMB_WIDTH, THUMB_POSITION_RATIO
    global THUMB_FALLBACK_SECONDS, THUMB_EXTRACT_TIMEOUT, PROBE_TIMEOUT
    global THUMB_CONCURRENCY, SCAN_BATCH, CELL_WIDTH, CELL_ASPECT
    global FILENAME_ROW, GRID_SPACING, MIN_COLS, MAX_COLS
    global DRAG_PREVIEW_WIDTH, DRAG_PREVIEW_HEIGHT
    global AUTOPLAY_DELAY_MS, SEEK_THROTTLE_MS, DOUBLE_CLICK_MAX_DIST
    global DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT, SEARCH_BOX_WIDTH
    global SEARCH_DEBOUNCE_MS
    global SIDEBAR_WIDTH, ROTATE_USE_CUDA, ASPECT_USE_CUDA, CONVERT_USE_CUDA
    global CACHE_DIR

    _settings_data.clear()
    _settings_data.update(_read_settings_file())

    SUPPORTED_EXTENSIONS = _extensions()
    CACHE_DIR = _cache_dir()
    THUMB_WIDTH = _num("thumb_width", int, minimum=1)
    THUMB_POSITION_RATIO = _num(
        "thumb_position_ratio", float, minimum=0.0, maximum=1.0
    )
    THUMB_FALLBACK_SECONDS = _num("thumb_fallback_seconds", float, minimum=0.0)
    THUMB_EXTRACT_TIMEOUT = _num("thumb_extract_timeout", int, minimum=1)
    PROBE_TIMEOUT = _num("probe_timeout", int, minimum=1)
    THUMB_CONCURRENCY = _thumb_concurrency()
    SCAN_BATCH = _num("scan_batch", int, minimum=1)
    CELL_WIDTH = _num("cell_width", int, minimum=1)
    CELL_ASPECT = _num("cell_aspect", float, minimum=0.01)
    FILENAME_ROW = _num("filename_row", int, minimum=0)
    GRID_SPACING = _num("grid_spacing", int, minimum=0)
    MIN_COLS = _num("min_cols", int, minimum=1)
    MAX_COLS = _num("max_cols", int, minimum=1)
    if MAX_COLS < MIN_COLS:
        log.warning(
            "settings: max_cols=%s is below min_cols=%s, using min_cols",
            MAX_COLS,
            MIN_COLS,
        )
        MAX_COLS = MIN_COLS
    DRAG_PREVIEW_WIDTH = _num("drag_preview_width", int, minimum=48)
    DRAG_PREVIEW_HEIGHT = _num("drag_preview_height", int, minimum=16)
    AUTOPLAY_DELAY_MS = _num("autoplay_delay_ms", int, minimum=0)
    # Floor of 1 ms: 0 would disable the throttle entirely (rule 5 caps
    # scrubbing at ~30 setPosition/s), so it is clamped rather than allowed.
    SEEK_THROTTLE_MS = _num("seek_throttle_ms", int, minimum=1)
    DOUBLE_CLICK_MAX_DIST = _num("double_click_max_dist", int, minimum=0)
    DEFAULT_WINDOW_WIDTH = _num("default_window_width", int, minimum=200)
    DEFAULT_WINDOW_HEIGHT = _num("default_window_height", int, minimum=200)
    SEARCH_BOX_WIDTH = _num("search_box_width", int, minimum=120)
    SEARCH_DEBOUNCE_MS = _num("search_debounce_ms", int, minimum=0)
    SIDEBAR_WIDTH = _num("sidebar_width", int, minimum=120)
    ROTATE_USE_CUDA = _bool("rotate_use_cuda")
    ASPECT_USE_CUDA = _bool("aspect_use_cuda")
    CONVERT_USE_CUDA = _bool("convert_use_cuda")


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

    ``cache_dir`` in settings.yml selects a custom location. The environment
    override remains higher priority for isolated tests and compatibility.
    """
    override = os.environ.get("VIDEO_PREVIEWER_CACHE_DIR")
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    if CACHE_DIR is not None:
        return CACHE_DIR
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
