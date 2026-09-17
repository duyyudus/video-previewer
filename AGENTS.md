# AGENTS.md — video-previewer

Guidance for AI coding agents (and humans) working in this repository.

## What this project is

A minimal, cross-platform desktop app (Windows / Linux / macOS) that browses a
local folder of videos as a YouTube-like responsive thumbnail grid. Hovering a
tile plays a short muted looping preview on **one shared** `QMediaPlayer`;
sweeping the pointer horizontally across the tile scrubs the video.
Built for personal use: simple, fast, low on resources.

Python 3.12+, PySide6 (Qt 6), `ffmpeg`/`ffprobe` on `PATH`.

## Commands

```bash
uv sync                 # create .venv and install deps (incl. dev group)
uv run video-preview    # run the app (or double-click run.bat on Windows)
uv run pytest           # run the test suite (headless)
uv run ruff check       # lint (rule set deliberately minimal, see pyproject.toml)
uv run python scripts/render_check.py   # offscreen smoke check; writes
                                        # scripts/screenshot-offscreen.png
```

- **Always go through the project venv.** Prefer the venv interpreter directly
  (`.venv\Scripts\python.exe -m pytest`, `.venv\Scripts\video-preview.exe`)
  over bare `uv run`/`uvx` — `uv` may touch caches outside the workspace and
  get blocked in sandboxed shells, while the venv lives inside the repo.

- FFmpeg is a hard runtime requirement (thumbnails, metadata). Pipeline tests
  are **skipped** when `ffmpeg`/`ffprobe` are missing — a green run with many
  skips may mean ffmpeg is not installed.
- Overrides: `VIDEO_PREVIEWER_FFMPEG`, `VIDEO_PREVIEWER_FFPROBE` (binary
  locations), `VIDEO_PREVIEWER_CACHE_DIR` (per-user cache directory; the
  `cache_dir` test fixture sets this), `VIDEO_PREVIEWER_SETTINGS` (settings
  file location; defaults to `settings.yml` in the project root).

## Repository layout

```
settings.yml           # user-adjustable tunables (loaded by config.py)
src/video_previewer/
├── main.py            # entry point: logging setup, C-stderr redirect to
│                      #   <cache_dir>/console.log, QT_LOGGING_RULES
├── app.py             # QApplication bootstrap, dark palette, run()
├── config.py          # settings.yml loader + built-in defaults +
│                      #   path/env helpers
├── open_external.py   # open a video with the OS-default player (fail soft)
├── ui/
│   ├── main_window.py # MainWindow: folder selection, state, exit dialog
│   ├── video_grid.py  # VideoGrid (QListView) + responsive column layout
│   ├── video_delegate.py  # tile painting (thumbnail + filename), hover/scrub
│   ├── folder_sidebar.py  # folder tree sidebar (QTreeView + QFileSystemModel);
│   │                  #   double-click loads a folder, single click never does
│   └── exit_dialog.py # keep/discard prompt on close
├── models/
│   ├── video_item.py  # VideoItem dataclass; video_id() = sha1(path|size|mtime)
│   ├── sorting.py     # SortKey/SortOrder (name, date modified; asc/desc) +
│   │                  #   the sort-key derivation shared by model and menu
│   └── video_model.py # QAbstractListModel (keeps its rows in sort order;
│                      #   reorders with layoutChanged + remapped persistent
│                      #   indices so the view keeps its scroll position; a
│                      #   scan defers the reorder until it finishes)
├── media/
│   ├── metadata.py    # ffprobe probing (duration, size, vcodec)
│   ├── thumbnailer.py # ffmpeg frame extraction (~15% into the video)
│   ├── player.py      # PreviewPlayer: the ONE shared QMediaPlayer
│   └── seek_bar.py    # SeekBarOverlay timeline bar over the hovered tile
├── cache/
│   ├── database.py    # SQLite (WAL, lock-guarded) — videos + scans tables
│   └── cache.py       # ThumbnailCache: hydrate/store/purge policy
└── workers/
    ├── scanner.py     # QRunnable folder walk, emits batches of SCAN_BATCH
    └── thumbnail_worker.py  # ThumbnailQueue: bounded, deduplicated
                             #   QThreadPool jobs (THUMB_CONCURRENCY)

tests/                 # pytest suite, see "Testing" below
scripts/render_check.py
```

## Hard architectural rules (do not violate)

1. **Exactly one `QMediaPlayer` + one video output widget for the whole app.**
   Hovering a tile moves that single widget over the tile. Never create a
   player per tile/item.
2. **Model/view grid.** `QListView` + `QAbstractListModel` + custom delegate;
   no per-video `QWidget`s; the grid must stay responsive with 10k+ files.
3. **No blocking of the GUI thread.** Scanning and thumbnail/metadata work run
   on `QThreadPool`/`QRunnable` with signals back to the GUI thread. The GUI
   thread only renders, handles pointer events, updates the model, and
   controls the player.
4. **Cache expensive work.** Thumbnails (JPEG) + metadata (SQLite) persist per
   user; a file is re-processed only when its size or mtime changes
   (`VideoItem.vid` identity). Scan results are cached per (folder,
   recursive) key. Files that ffmpeg refuses are negatively cached so a broken
   file is not re-probed on every scan.
5. **Throttle scrubbing.** `setPosition` at most ~30/s (`SEEK_THROTTLE_MS`),
   one pending seek flushed by a single-shot `QTimer`.
6. **Isolate the playback backend.** All Qt Multimedia coupling lives in
   `media/player.py` (+ `seek_bar.py`); grid, cache, scanner, and thumbnail
   pipeline must stay backend-agnostic so it can be swapped for libmpv later.
7. **Minimalism is a requirement.** No playlists, tags, ratings, accounts,
   settings UI, or media-library features unless explicitly requested. Keep
   modules small and avoid unnecessary abstraction.
8. **Fail soft.** A bad file must never crash the app (preview falls back to
   the static thumbnail; DB access after close raises only inside the
   wrapper; ffmpeg invocations have timeouts).

## Conventions

- `from __future__ import annotations` in every module; full type hints;
  `@dataclass(slots=True)` for data types.
- **Tunables live in `settings.yml`** (project root) — thumbnail
  geometry/timeout/concurrency, scan batch size, grid metrics, autoplay
  delay, seek throttle, double-click drift limit, default window size, sidebar
  width, supported extensions. `config.py` loads them with built-in defaults
  (fail soft; out-of-range numbers are clamped) and re-exports them as module
  constants; feature code keeps reading `config.X`. Do not scatter magic
  numbers into feature code.
- **Rows may move on any mutation.** The model keeps its rows in sort order,
  so a row number is only valid until the next mutation: resolve it with
  `VideoModel.row_for()` immediately before using it, and hand batches of
  updates to `update_items()` (path-based) instead of looping over
  `update_item()`. Scan batches run with the reorder deferred
  (`set_layout_deferred`) and settle once when the scan finishes — resorting
  per batch is quadratic in the file count.
- Concurrency: Qt-native only (`QThreadPool`, `QRunnable`, signals/slots).
  Do **not** introduce asyncio.
- The shared SQLite connection is created with `check_same_thread=False` and
  every access goes through the `Database` lock (WAL mode). Keep it that way.
- Logging: Python `logging` to stdout; Qt/FFmpeg C-level stderr is redirected
  to `<cache_dir>/console.log` in `main.py` (never "fix" a user's console by
  undoing that). `QT_LOGGING_RULES` is set at the top of `main.py` and must
  remain set **before any PySide6 import**.
- Path handling: `normalize_path()` (case-insensitive on Windows) and
  `as_posix()` forms are what get stored in SQLite; `video_id()` derivation
  must stay stable or the cache silently invalidates.
- Platform notes: the exit prompt (`exit_dialog.ask_keep_on_exit`) guards
  `closeEvent`; the last folder + recursive toggle persist via `QSettings`,
  as do the window geometry, the sidebar toggle, and its split width, and the
  grid's sort key + order (`config.SETTING_SORT_KEY` / `SETTING_SORT_ORDER`).

## Commit messages

Follow the `task-type: message` format — a short lowercase task-type prefix,
a colon, a space, then a concise description of the change:

```
fix: prevent crash when a video file is deleted mid-preview
ui: add subfolders checkbox to the toolbar
```

Task-types: `feat:` (new functionality), `fix:` (bug fixes), `ui:` (UI
changes), `test:` (test-only changes), `docs:` (documentation), `perf:`
(performance), `chore:` (build/tooling/maintenance). Use `ui:` for anything
visible in the window, even if it also touches logic.

## Testing

- Headless by design: `tests/conftest.py` sets
  `QT_QPA_PLATFORM=offscreen` **before any `QApplication` is created** — keep
  that ordering if you touch conftest.
- Session-scoped `qapp` fixture uses the test-only identity
  `video-previewer-test` so `QSettings` never touches real user app data.
- Use the `cache_dir` fixture (isolates the cache via
  `VIDEO_PREVIEWER_CACHE_DIR`) for anything that touches the cache/DB.
- `make_video()` (conftest) generates tiny real videos with ffmpeg; the
  pipeline tests skip themselves when ffmpeg is unavailable.
- `pump(app, condition)` spins the event loop until a condition holds — use it
  to wait for async signals in tests instead of sleeping.
- Autouse fixtures: `_clean_settings` clears every persisted `QSettings` key
  (last folder, recursive, window geometry, sidebar visibility and split)
  between tests, and `_no_exit_prompt` stubs `exit_dialog.ask_keep_on_exit` to
  `False` so `closeEvent` never blocks. Tests that exercise the "keep"
  outcome must re-stub `ask_keep_on_exit` themselves.
- Windows/DSH quirk (keep if editing conftest): when tests run inside a DSH
  sandbox, directories created with an explicit `mode` argument become
  inaccessible afterwards, which breaks pytest's tmpdir plugin — conftest
  wraps `os.mkdir` to drop the `mode` argument on Windows.
- A typical new feature should add tests under `tests/` in the same style;
  verify with `uv run pytest` and, for visual changes, re-run
  `scripts/render_check.py` and inspect `scripts/screenshot-offscreen.png`.

## Debugging pointers

- App log lines → stdout (console). Native/FFmpeg C stderr →
  `<cache_dir>/console.log` (Windows: `%LOCALAPPDATA%\video-previewer\`,
  macOS: `~/Library/Caches/video-previewer/`, Linux: `~/.cache/video-previewer/`).
- Thumbnail/metadata pipeline failures are logged, not raised; check the log
  before re-running ffmpeg by hand.
- `scripts/render_check.py` exercises the real `MainWindow` offscreen and
  reports how many thumbnails became ready in 60 s — a quick end-to-end
  regression check.
