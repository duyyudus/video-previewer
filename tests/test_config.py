"""settings.yml loading: overrides, defaults, and fail-soft behaviour."""

from __future__ import annotations

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
    assert config.SUPPORTED_EXTENSIONS == frozenset(
        {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
    )


def test_settings_file_overrides_values(tmp_path, monkeypatch):
    _load(
        tmp_path,
        monkeypatch,
        "thumb_width: 480\ncell_aspect: 1.0\n"
        "supported_extensions:\n  - MP4\n  - .ts\n",
    )
    assert config.THUMB_WIDTH == 480
    assert config.CELL_ASPECT == pytest.approx(1.0)
    # Extension entries are normalised to lowercase, dot included.
    assert config.SUPPORTED_EXTENSIONS == frozenset({".mp4", ".ts"})
    # Untouched keys keep their built-in defaults.
    assert config.SEEK_THROTTLE_MS == 33
    assert config.THUMB_CONCURRENCY == 4


def test_invalid_values_fall_back_to_defaults(tmp_path, monkeypatch, caplog):
    _load(
        tmp_path,
        monkeypatch,
        "thumb_width: not-a-number\n"
        "thumb_concurrency: true\n"
        "supported_extensions: mp4\n",
    )
    assert config.THUMB_WIDTH == 320
    assert config.THUMB_CONCURRENCY == 4
    assert ".mp4" in config.SUPPORTED_EXTENSIONS
    assert any("settings" in r.message for r in caplog.records)


def test_unparseable_yaml_falls_back_to_defaults(tmp_path, monkeypatch):
    _load(tmp_path, monkeypatch, "thumb_width: [unclosed\n")
    assert config.THUMB_WIDTH == 320
    assert config.SEEK_THROTTLE_MS == 33


def test_shipped_settings_yaml_is_sane():
    # No env override: reads <repo_root>/settings.yml.
    config.load_settings()
    assert config.settings_path().name == "settings.yml"
    assert config.settings_path().is_file()
    assert config.THUMB_WIDTH > 0
    assert 0.0 < config.THUMB_POSITION_RATIO <= 1.0
    assert config.MIN_COLS <= config.MAX_COLS
    assert config.MIN_COLS >= 1
    assert config.SEEK_THROTTLE_MS >= 1
    assert config.SUPPORTED_EXTENSIONS
    assert all(e.startswith(".") for e in config.SUPPORTED_EXTENSIONS)
