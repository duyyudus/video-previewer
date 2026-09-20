"""settings.yml loading: overrides, defaults, and fail-soft behaviour."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from video_previewer import config


@pytest.fixture(autouse=True)
def _restore_project_settings():
    """Leave the config module in its shipped state for the rest of the suite."""
    yield
    import os

    os.environ.pop("VIDEO_PREVIEWER_SETTINGS", None)
    config.load_settings()


def _load(tmp_path: Path, monkeypatch, text: str) -> None:
    path = tmp_path / "settings.yml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv("VIDEO_PREVIEWER_SETTINGS", str(path))
    config.load_settings()


def test_missing_settings_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "VIDEO_PREVIEWER_SETTINGS", str(tmp_path / "missing.yml")
    )
    config.load_settings()
    assert config.THUMB_WIDTH == 320
    assert config.THUMB_POSITION_RATIO == pytest.approx(0.15)
    assert config.CELL_ASPECT == pytest.approx(9 / 16)
    assert config.SEEK_THROTTLE_MS == 33
    assert config.SEARCH_BOX_WIDTH == 310
    assert config.SEARCH_DEBOUNCE_MS == 150
    assert config.DRAG_PREVIEW_WIDTH == 160
    assert config.DRAG_PREVIEW_HEIGHT == 28
    assert config.SUPPORTED_EXTENSIONS == frozenset(
        {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
    )


def test_settings_file_overrides_values(tmp_path, monkeypatch):
    _load(
        tmp_path,
        monkeypatch,
        "thumb_width: 480\ncell_aspect: 1.0\n"
        "search_box_width: 360\nsearch_debounce_ms: 80\n"
        "supported_extensions:\n  - MP4\n  - .ts\n",
    )
    assert config.THUMB_WIDTH == 480
    assert config.CELL_ASPECT == pytest.approx(1.0)
    assert config.SEARCH_BOX_WIDTH == 360
    assert config.SEARCH_DEBOUNCE_MS == 80
    # Extension entries are normalised to lowercase, dot included.
    assert config.SUPPORTED_EXTENSIONS == frozenset({".mp4", ".ts"})
    # Untouched keys keep their built-in defaults.
    assert config.SEEK_THROTTLE_MS == 33
    assert config.THUMB_CONCURRENCY == max(1, os.cpu_count() or 1)


def test_settings_file_configures_relative_cache_dir(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, "cache_dir: local-cache\n")
    assert config.app_cache_dir() == (tmp_path / "local-cache").resolve()
    assert config.thumbnail_dir() == (
        tmp_path / "local-cache" / "thumbnails"
    ).resolve()
    assert config.database_path() == (
        tmp_path / "local-cache" / "metadata.sqlite"
    ).resolve()


def test_cache_dir_environment_override_remains_highest_priority(
    tmp_path, monkeypatch
):
    _load(tmp_path, monkeypatch, "cache_dir: yaml-cache\n")
    override = tmp_path / "isolated-cache"
    monkeypatch.setenv("VIDEO_PREVIEWER_CACHE_DIR", str(override))
    assert config.app_cache_dir() == override


def test_invalid_values_fall_back_to_defaults(tmp_path, monkeypatch, caplog):
    _load(
        tmp_path,
        monkeypatch,
        "thumb_width: not-a-number\n"
        "thumb_concurrency: true\n"
        "cache_dir: [not, a, path]\n"
        "supported_extensions: mp4\n",
    )
    assert config.THUMB_WIDTH == 320
    assert config.THUMB_CONCURRENCY == max(1, os.cpu_count() or 1)
    assert config.CACHE_DIR is None
    assert ".mp4" in config.SUPPORTED_EXTENSIONS
    assert any("settings" in r.message for r in caplog.records)


def test_unparseable_yaml_falls_back_to_defaults(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, "thumb_width: [unclosed\n")
    assert config.THUMB_WIDTH == 320
    assert config.SEEK_THROTTLE_MS == 33


def test_null_thumbnail_concurrency_uses_logical_cpu_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "cpu_count", lambda: 16)
    _load(tmp_path, monkeypatch, "thumb_concurrency: null\n")
    assert config.THUMB_CONCURRENCY == 16


def test_null_thumbnail_concurrency_survives_unknown_cpu_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "cpu_count", lambda: None)
    _load(tmp_path, monkeypatch, "thumb_concurrency: null\n")
    assert config.THUMB_CONCURRENCY == 1


def test_explicit_thumbnail_concurrency_overrides_cpu_count(tmp_path, monkeypatch):
    monkeypatch.setattr(config.os, "cpu_count", lambda: 16)
    _load(tmp_path, monkeypatch, "thumb_concurrency: 6\n")
    assert config.THUMB_CONCURRENCY == 6


def test_out_of_range_values_are_clamped(tmp_path, monkeypatch, caplog):
    # A zero concurrency would start no thumbnail job ever; a zero batch
    # raises ValueError inside the scanner's range() batching; a ratio
    # outside [0, 1] seeks outside the video.
    _load(
        tmp_path,
        monkeypatch,
        "thumb_concurrency: 0\n"
        "scan_batch: 0\n"
        "thumb_position_ratio: 2.0\n"
        "min_cols: -3\n"
        "max_cols: 0\n"
        "seek_throttle_ms: -5\n",
    )
    assert config.THUMB_CONCURRENCY == 1
    assert config.SCAN_BATCH == 1
    assert config.THUMB_POSITION_RATIO == pytest.approx(1.0)
    assert config.MIN_COLS == 1
    assert config.MAX_COLS == 1
    # Floor is 1, not 0: a zero throttle would disable rule 5's seek cap.
    assert config.SEEK_THROTTLE_MS == 1
    assert any("clamped" in r.getMessage() for r in caplog.records)


def test_inverted_column_bounds_are_reconciled(tmp_path, monkeypatch, caplog):
    _load(tmp_path, monkeypatch, "min_cols: 8\nmax_cols: 2\n")
    assert config.MIN_COLS == 8
    assert config.MAX_COLS == 8
    assert any("max_cols" in r.getMessage() for r in caplog.records)


def test_shipped_settings_yaml_is_sane():
    # No env override: reads <repo_root>/settings.yml.
    config.load_settings()
    assert config.settings_path().name == "settings.yml"
    assert config.settings_path().is_file()
    assert config.CACHE_DIR is None
    assert config.THUMB_WIDTH > 0
    assert 0.0 < config.THUMB_POSITION_RATIO <= 1.0
    assert config.MIN_COLS <= config.MAX_COLS
    assert config.MIN_COLS >= 1
    assert config.SEEK_THROTTLE_MS >= 1
    assert config.DRAG_PREVIEW_WIDTH >= 48
    assert config.DRAG_PREVIEW_HEIGHT >= 16
    assert config.SUPPORTED_EXTENSIONS
    assert all(e.startswith(".") for e in config.SUPPORTED_EXTENSIONS)
