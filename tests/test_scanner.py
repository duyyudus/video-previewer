"""Scanner cancellation (audit L8) and shutdown safety.

The generation guard only protects the *receiver*; a superseded scan used to
keep os.walk-ing and stat()-ing the old tree (minutes of wasted IO on a
network folder), still write its save_scan row, and emit into the void.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from video_previewer.cache.cache import ThumbnailCache
from video_previewer.cache.database import Database
from video_previewer.workers import scanner as scanner_module
from video_previewer.workers.scanner import Scanner, ScanSignals


@pytest.fixture
def db(cache_dir):
    d = Database(cache_dir / "metadata.sqlite")
    yield d
    d.close()


def _folder(tmp_path) -> Path:
    folder = tmp_path / "vids"
    folder.mkdir()
    (folder / "a.mp4").write_bytes(b"x" * 16)
    (folder / "b.mp4").write_bytes(b"y" * 16)
    return folder


def _run(db, folder: Path, recursive: bool, is_stale):
    signals = ScanSignals()
    items: list = []
    finished: list = []
    errors: list = []
    signals.items.connect(lambda chunk, gen: items.extend(chunk))
    signals.finished.connect(lambda result, gen: finished.append(result))
    signals.error.connect(errors.append)
    scanner = Scanner(
        folder, recursive, db, ThumbnailCache(db), signals, 1, is_stale=is_stale
    )
    scanner.run()  # direct: signals are same-thread direct connections
    return items, finished, errors


def test_scan_with_current_generation_unaffected(db, tmp_path):
    folder = _folder(tmp_path)
    items, finished, errors = _run(db, folder, recursive=False, is_stale=lambda: False)
    assert errors == []
    assert len(items) == 2
    assert len(finished) == 1 and finished[0].count == 2
    assert db.load_scan(db.scan_key(folder, False)) is not None


def test_scan_without_cancellation_hook_still_works(db, tmp_path):
    folder = _folder(tmp_path)
    items, finished, errors = _run(db, folder, recursive=False, is_stale=None)
    assert errors == [] and len(items) == 2 and len(finished) == 1


def test_superseded_scan_is_abandoned_silently(db, tmp_path):
    folder = _folder(tmp_path)
    items, finished, errors = _run(db, folder, recursive=True, is_stale=lambda: True)
    # no emission into the void, no error shown in the UI...
    assert items == [] and finished == [] and errors == []
    # ...and no wasted scan-cache row for the abandoned scan
    assert db.load_scan(db.scan_key(folder, True)) is None


def test_superseded_mid_walk_aborts_before_save(db, tmp_path, monkeypatch):
    # Go stale *inside* the walk (as soon as the first directory is produced)
    # instead of counting internal check calls: the abort must come from the
    # in-walk check, whatever the number of check sites turns out to be.
    folder = _folder(tmp_path)
    for name in ("sub1", "sub2"):
        (folder / name).mkdir()
        (folder / name / "c.mp4").write_bytes(b"z" * 16)

    stale = {"on": False}
    entered = {"dirs": 0}
    real_walk = scanner_module.os.walk

    def walk_spy(top, *args, **kwargs):
        for entry in real_walk(top, *args, **kwargs):
            entered["dirs"] += 1
            stale["on"] = True  # superseded after the walk has started
            yield entry

    monkeypatch.setattr(scanner_module.os, "walk", walk_spy)

    items, finished, errors = _run(db, folder, recursive=True,
                                   is_stale=lambda: stale["on"])

    assert entered["dirs"] == 1  # the walk really started...
    assert items == [] and finished == [] and errors == []  # ...then aborted
    assert db.load_scan(db.scan_key(folder, True)) is None


def test_closed_database_aborts_scan_without_a_ui_error(db, tmp_path):
    # The window closed mid-walk: the DB is gone, so the scan must log and
    # stop — not surface "Scan failed" in a window that is tearing down.
    folder = _folder(tmp_path)
    db.close()

    signals = ScanSignals()
    errors: list[str] = []
    signals.error.connect(errors.append)
    Scanner(folder, False, db, ThumbnailCache(db), signals, 1).run()

    assert errors == []
