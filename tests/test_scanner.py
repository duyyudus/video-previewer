"""Scanner cancellation (audit M2) and shutdown safety.

The generation guard only protects the *receiver*; a superseded scan used to
keep scandir-ing and stat()-ing the old tree (minutes of wasted IO on a
network folder), still write its save_scan row, and emit into the void.
"""

from __future__ import annotations

import logging
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
    real_scandir = scanner_module.os.scandir

    def scandir_spy(path, *args, **kwargs):
        entered["dirs"] += 1
        stale["on"] = True  # superseded after the walk has started
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "scandir", scandir_spy)

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


def test_recursive_scan_reports_unreadable_root(db, tmp_path, monkeypatch):
    # os.walk used to swallow this: the user saw a silently empty grid for
    # a folder they could not read, while the flat path raised.
    folder = _folder(tmp_path)

    def denied(path, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(scanner_module.os, "scandir", denied)
    items, finished, errors = _run(db, folder, recursive=True,
                                   is_stale=lambda: False)
    assert items == [] and finished == []
    assert len(errors) == 1 and "cannot read folder" in errors[0]


def test_unreadable_subfolder_is_logged_and_skipped(db, tmp_path, monkeypatch,
                                                    caplog):
    folder = _folder(tmp_path)
    locked = folder / "locked"
    locked.mkdir()
    (locked / "c.mp4").write_bytes(b"z" * 16)

    real_scandir = scanner_module.os.scandir

    def selective_scandir(path, *args, **kwargs):
        if Path(path).name == "locked":
            raise PermissionError("denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "scandir", selective_scandir)
    with caplog.at_level(logging.WARNING):
        items, finished, errors = _run(db, folder, recursive=True,
                                       is_stale=lambda: False)
    # The rest of the tree still scans...
    assert errors == [] and len(items) == 2 and finished[0].count == 2
    # ...but the skipped directory leaves a trace in the log.
    assert any("skipping unreadable folder" in r.getMessage()
               for r in caplog.records)
