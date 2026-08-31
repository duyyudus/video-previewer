"""VideoModel: insertion, dedup, in-place updates, roles."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import Qt

from video_previewer.models.video_item import VideoItem
from video_previewer.models.video_model import VideoModel


@pytest.fixture
def model(qapp):
    m = VideoModel()
    yield m
    m.deleteLater()


def _item(name, mtime=1000.0, size=100):
    return VideoItem(Path("/v") / name, size, mtime)


def test_add_and_dedup(model):
    a = _item("a.mp4")
    assert model.add_items([a]) == [0]
    # duplicate path is skipped
    assert model.add_items([_item("a.mp4")]) == []
    assert model.add_items([_item("b.mkv")]) == [1]
    assert model.count() == 2
    assert model.row_for(Path("/v/a.mp4")) == 0
    assert model.row_for(Path("/v/b.mkv")) == 1


def test_update_item_preserves_row(model):
    a = _item("a.mp4")
    model.add_items([a])
    updated = a.with_metadata(1200, 640, 360).with_thumbnail(Path("/t/a.jpg"))
    model.update_item(0, updated)
    item = model.item_at(0)
    assert item.duration_ms == 1200
    assert item.thumb_ready is True
    assert model.row_for(Path("/v/a.mp4")) == 0
    assert model.count() == 1


def test_roles(model):
    a = _item("a.mp4").with_metadata(9000, 1920, 1080, "hevc").with_thumbnail(Path("/t.jpg"))
    model.add_items([a])
    idx = model.index(0)
    assert model.data(idx, Qt.DisplayRole) == "a.mp4"
    assert model.data(idx, VideoModel.PathRole) == Path("/v/a.mp4")
    assert model.data(idx, VideoModel.DurationRole) == 9000
    assert model.data(idx, VideoModel.WidthRole) == 1920
    assert model.data(idx, VideoModel.ThumbReadyRole) is True
    assert model.data(idx, VideoModel.ThumbnailPathRole) == Path("/t.jpg")
    assert isinstance(model.data(idx, VideoModel.VidRole), str)
    # not selectable
    assert bool(model.flags(idx) & Qt.ItemFlag.ItemIsSelectable) is False
    assert bool(model.flags(idx) & Qt.ItemFlag.ItemIsEnabled) is True


def test_clear(model):
    model.add_items([_item("a.mp4"), _item("b.mp4")])
    model.clear()
    assert model.count() == 0
    assert model.row_for(Path("/v/a.mp4")) is None
