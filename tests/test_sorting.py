"""Sorting: natural name keys, model ordering, layout signalling."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QPersistentModelIndex

from video_previewer.models.sorting import SortKey, SortOrder, natural_key
from video_previewer.models.video_item import VideoItem
from video_previewer.models.video_model import VideoModel


@pytest.fixture
def model(qapp):
    m = VideoModel()
    yield m
    m.deleteLater()


def _item(name: str, mtime: float = 1000.0) -> VideoItem:
    return VideoItem(Path("/v") / name, 100, mtime)


def _names(model: VideoModel) -> list[str]:
    return [model.item_at(row).filename for row in range(model.count())]


def test_natural_key_orders_numbers_as_numbers():
    names = ["ep10.mp4", "ep2.mp4", "ep1.mp4"]
    assert sorted(names, key=natural_key) == ["ep1.mp4", "ep2.mp4", "ep10.mp4"]
    # case-insensitive, like every other name sort users expect
    assert sorted(["B.mp4", "a.mp4"], key=natural_key) == ["a.mp4", "B.mp4"]


def test_default_sort_is_name_ascending(model):
    assert model.sort_key is SortKey.NAME
    assert model.sort_order is SortOrder.ASC
    model.add_items([_item("b.mp4"), _item("a10.mp4"), _item("a2.mp4")])
    assert _names(model) == ["a2.mp4", "a10.mp4", "b.mp4"]


def test_sort_by_modified_both_directions(model):
    model.add_items(
        [_item("old.mp4", 100.0), _item("mid.mp4", 200.0), _item("new.mp4", 300.0)]
    )
    model.set_sort(SortKey.MODIFIED, SortOrder.ASC)
    assert _names(model) == ["old.mp4", "mid.mp4", "new.mp4"]
    model.set_sort(SortKey.MODIFIED, SortOrder.DESC)
    assert _names(model) == ["new.mp4", "mid.mp4", "old.mp4"]


def test_sort_by_name_descending(model):
    model.add_items([_item("a.mp4"), _item("c.mp4"), _item("b.mp4")])
    model.set_sort(SortKey.NAME, SortOrder.DESC)
    assert _names(model) == ["c.mp4", "b.mp4", "a.mp4"]


def test_set_sort_accepts_persisted_strings(model):
    # values come back from QSettings as plain strings
    model.add_items([_item("a.mp4"), _item("b.mp4")])
    model.set_sort("modified", "desc")
    assert model.sort_key is SortKey.MODIFIED
    assert model.sort_order is SortOrder.DESC


def test_set_sort_rejects_unknown_values(model):
    with pytest.raises(ValueError):
        model.set_sort("size", "asc")


def test_same_name_tie_broken_by_path(model):
    model.add_items([VideoItem(Path("/z/x.mp4"), 1, 5.0), VideoItem(Path("/a/x.mp4"), 1, 5.0)])
    assert [model.item_at(0).path, model.item_at(1).path] == [
        Path("/a/x.mp4"),
        Path("/z/x.mp4"),
    ]


def test_row_lookup_and_update_survive_a_resort(model):
    a = _item("a.mp4")
    model.add_items([_item("c.mp4"), a, _item("b.mp4")])
    row = model.row_for(Path("/v/a.mp4"))
    assert row == 0
    model.update_item(row, a.with_metadata(9000, 640, 360))
    assert model.item_at(model.row_for(Path("/v/a.mp4"))).duration_ms == 9000
    assert model.count() == 3
    assert _names(model) == ["a.mp4", "b.mp4", "c.mp4"]


def test_add_items_returns_the_final_rows(model):
    # the caller uses the returned rows to queue thumbnails straight away
    added = model.add_items([_item("c.mp4"), _item("a.mp4")])
    assert [model.item_at(row).filename for row in added] == ["c.mp4", "a.mp4"]


def test_ordered_append_does_not_relayout(model):
    # The scanner delivers name-ascending batches: with the default sort in
    # place nothing moves, so a 10k-file scan must not churn the view.
    layouts: list[bool] = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.add_items([_item("a.mp4"), _item("b.mp4")])
    model.add_items([_item("c.mp4"), _item("d.mp4")])
    assert layouts == []
    model.add_items([_item("aa.mp4")])  # lands in the middle this time
    assert layouts == [True]


def test_resort_remaps_persistent_indices(model):
    model.add_items([_item("a.mp4"), _item("b.mp4")])
    index = QPersistentModelIndex(model.index(0))
    assert index.data() == "a.mp4"
    model.set_sort(SortKey.NAME, SortOrder.DESC)
    assert index.row() == 1
    assert index.data() == "a.mp4"  # still the same item, on a new row


def test_metadata_update_moves_the_row_when_the_key_changes(model):
    model.set_sort(SortKey.MODIFIED, SortOrder.ASC)
    model.add_items([_item("a.mp4", 100.0), _item("b.mp4", 200.0)])
    assert _names(model) == ["a.mp4", "b.mp4"]
    # the file was touched on disk, so the next scan reports a newer mtime
    model.update_item(0, VideoItem(Path("/v/a.mp4"), 100, 900.0))
    assert _names(model) == ["b.mp4", "a.mp4"]
    assert model.row_for(Path("/v/a.mp4")) == 1


def test_metadata_update_keeps_the_row_when_the_key_is_unchanged(model):
    layouts: list[bool] = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.set_sort(SortKey.MODIFIED, SortOrder.DESC)
    model.add_items([_item("a.mp4", 100.0), _item("b.mp4", 200.0)])
    layouts.clear()
    model.update_item(0, model.item_at(0).with_thumbnail(Path("/t.jpg")))
    assert layouts == []
    assert _names(model) == ["b.mp4", "a.mp4"]


def test_update_items_applies_by_path_and_reorders_once(model):
    # A scan batch that updates several files must not carry row numbers
    # across mutations: the first update reorders the rows, so the row of the
    # second one is already a different file by then.
    layouts: list[bool] = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.set_sort(SortKey.MODIFIED, SortOrder.ASC)
    model.add_items([_item("x.mp4", 100.0), _item("y.mp4", 1000.0), _item("z.mp4", 2000.0)])
    layouts.clear()
    model.update_items([_item("x.mp4", 3000.0), _item("z.mp4", 1500.0)])
    assert _names(model) == ["y.mp4", "z.mp4", "x.mp4"]
    assert model.row_for(Path("/v/x.mp4")) == 2
    assert layouts == [True]  # one relayout for the whole batch


def test_update_items_skips_files_that_are_gone(model):
    model.add_items([_item("a.mp4"), _item("b.mp4")])
    model.update_items([_item("gone.mp4"), _item("a.mp4", 999.0)])
    assert _names(model) == ["a.mp4", "b.mp4"]
    assert model.count() == 2


def test_update_that_moves_a_row_does_not_also_emit_data_changed(model):
    # layoutChanged repaints everything; a dataChanged for the row the item
    # just left would point at whoever moved into it.
    changes: list[int] = []
    model.dataChanged.connect(lambda tl, br, roles=None: changes.append(tl.row()))
    model.set_sort(SortKey.MODIFIED, SortOrder.ASC)
    model.add_items([_item("a.mp4", 100.0), _item("b.mp4", 200.0)])
    changes.clear()
    model.update_item(0, _item("a.mp4", 900.0))  # a moves to the back
    assert changes == []
    assert _names(model) == ["b.mp4", "a.mp4"]


def test_update_that_keeps_its_row_still_notifies_the_view(model):
    # A re-scanned file has a fresh mtime *and* no thumbnail yet, so the tile
    # must repaint even when the new mtime leaves it in the same place.
    changes: list[int] = []
    layouts: list[bool] = []
    model.dataChanged.connect(lambda tl, br, roles=None: changes.append(tl.row()))
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.set_sort(SortKey.MODIFIED, SortOrder.ASC)
    model.add_items([_item("a.mp4", 100.0), _item("b.mp4", 900.0)])
    changes.clear()
    model.update_item(0, _item("a.mp4", 200.0))  # still first
    assert changes == [0]
    assert layouts == []


def test_deferred_layout_reorders_once_at_the_end(model):
    # A scan delivers a big folder in small batches; reordering per batch is
    # quadratic, so the order is applied once when the scan ends.
    layouts: list[bool] = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.set_layout_deferred(True)
    model.add_items([_item("c.mp4"), _item("a.mp4")])
    model.add_items([_item("b.mp4")])
    assert layouts == []
    assert _names(model) == ["c.mp4", "a.mp4", "b.mp4"]  # arrival order for now
    assert model.row_for(Path("/v/b.mp4")) == 2  # rows stay valid while deferred
    model.set_layout_deferred(False)
    assert layouts == [True]
    assert _names(model) == ["a.mp4", "b.mp4", "c.mp4"]
    assert model.row_for(Path("/v/b.mp4")) == 1


def test_deferred_updates_still_notify_the_view(model):
    # Deferred means "not reordered yet", not "not painted": the tile content
    # still has to update in place while the scan runs.
    changes: list[int] = []
    layouts: list[bool] = []
    model.dataChanged.connect(lambda tl, br, roles=None: changes.append(tl.row()))
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.set_sort(SortKey.MODIFIED, SortOrder.ASC)
    model.add_items([_item("a.mp4", 100.0), _item("b.mp4", 200.0)])
    model.set_layout_deferred(True)
    model.update_items([_item("a.mp4", 900.0)])
    assert layouts == []
    assert changes == [0]
    model.set_layout_deferred(False)
    assert _names(model) == ["b.mp4", "a.mp4"]


def test_explicit_sort_applies_even_while_deferred(model):
    # The user asked for a new order; waiting for a scan to finish would look
    # like the menu did nothing.
    layouts: list[bool] = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.add_items([_item("a.mp4"), _item("b.mp4")])
    model.set_layout_deferred(True)
    model.set_sort(SortKey.NAME, SortOrder.DESC)
    assert layouts == [True]
    assert _names(model) == ["b.mp4", "a.mp4"]
    model.set_layout_deferred(False)  # nothing left pending
    assert layouts == [True]


def test_clear_drops_a_pending_reorder(model):
    layouts: list[bool] = []
    model.layoutChanged.connect(lambda: layouts.append(True))
    model.set_layout_deferred(True)
    model.add_items([_item("b.mp4"), _item("a.mp4")])
    model.clear()
    model.set_layout_deferred(False)
    assert layouts == []
    assert model.add_items([_item("a.mp4")]) == [0]


def test_clear_empties_the_ordering_too(model):
    model.add_items([_item("b.mp4"), _item("a.mp4")])
    model.clear()
    assert model.count() == 0
    assert model.add_items([_item("a.mp4")]) == [0]