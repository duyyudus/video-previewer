# video-previewer

Minimalistic youtube-like video previewer.

Browse a folder of local videos as a responsive thumbnail grid; hover a tile
to watch a short muted preview; sweep the pointer horizontally across the
timeline bar at the tile's bottom edge to scrub through the video. Built for
personal use — simple, fast, and low on resources.

## How it works

* **Grid** — `QListView` + custom `QAbstractListModel` + custom delegate
  (model/view virtualization; only visible tiles are painted, so folders with
  thousands of videos stay responsive). Rows are kept in sort order by the
  model itself, so every consumer (hover, double-click, thumbnails) sees the
  same order the user does.
* **Folder navigation** — a toggleable sidebar (`QTreeView` over a
  directories-only `QFileSystemModel`) browses the filesystem; a single click
  only expands a folder, a **double-click** loads it into the grid. Dragging
  selected tiles onto a sidebar folder **moves** those video files there (the
  cached thumbnail and metadata migrate with the file).
* **Scanning** — folder walk runs on a worker thread and emits results in
  batches, so the grid populates incrementally while scanning continues.
  A persistent scan cache makes re-opening a folder instant.
* **Thumbnails** — one representative frame (≈15% into the video) extracted
  with `ffmpeg` on a bounded worker pool (4 at a time), decoded asynchronously.
  Metadata (`ffprobe`: duration, resolution, codec) and thumbnails are cached
  in SQLite + JPEG files; files are only re-processed when their size or
  modification time changes.
* **Hover preview** — exactly **one** shared `QMediaPlayer` for the whole
  app. Hovering a tile moves that single player's video output over the tile
  and starts muted, looping playback after a ~200 ms grace delay; leaving
  stops it and restores the static thumbnail.
* **Scrubbing** — inside the timeline bar at the bottom of the hovered
  tile, the horizontal pointer position maps to a video timestamp
  (`x / width × duration`) and is applied as a throttled `setPosition`
  (max ~30 seeks/second). The bar mirrors the pointer while you scrub and
  the real playback position while the preview loops.

## Requirements

* Python 3.12+
* [uv](https://docs.astral.sh/uv/)
* FFmpeg on `PATH` (`ffmpeg` + `ffprobe`) — e.g. `winget install Gyan.FFmpeg`
  on Windows. The binary locations can be overridden with
  `VIDEO_PREVIEWER_FFMPEG` / `VIDEO_PREVIEWER_FFPROBE`.

## Run

```bash
uv sync
uv run video-preview
```

Then click **Open Folder…** and pick a folder with videos.

## Usage notes

* Supported formats: `.mp4 .mkv .mov .webm .avi .m4v` (configurable in
  `settings.yml`).
* The **Subfolders** checkbox toggles recursive scanning (remembered).
* The **Sort** button orders the grid by **Name** (number-aware, so `ep2`
  comes before `ep10`) or **Date modified**, **Ascending** or **Descending**.
  Rows are reordered in place, so the scroll position survives the change and
  the hover preview simply stops; the choice is remembered. While a folder is
  still being scanned the grid fills in arrival order and settles into the
  sort order once the scan ends.
* Double-click a tile to open that video with your default player;
  double-clicking empty grid space opens the folder picker instead.
* **Drag to move** — drag one or more selected tiles onto a folder in the
  sidebar (the would-be target folder gets highlighted) to move the files
  there; their cached thumbnails move with them. Dragging onto a
  file-manager window (Explorer / Finder) copies or moves via the OS as
  usual; after a move the grid drops the gone tiles. Pressing on a tile's
  **timeline bar** stays a scrub gesture, so scrubbing never turns into an
  accidental drag — grab the thumbnail to drag.
* The **Sidebar** button shows or hides the folder tree; dragging the divider
  resizes it (both are remembered).
* The last opened folder and the window size/position are restored on next
  launch.
* Closing the window asks whether to **keep** the folder for next launch or
  **discard** it, which also drops its cached thumbnails and metadata.
* Tunables (thumbnail size, grid metrics, timeouts, concurrency, autoplay
  delay, seek throttle, supported extensions) live in `settings.yml` in the
  project root; restart the app after editing. Point
  `VIDEO_PREVIEWER_SETTINGS` at another file to override its location.
* Thumbnails/metadata are cached per user:
  * Windows: `%LOCALAPPDATA%\video-previewer\`
  * macOS: `~/Library/Caches/video-previewer/`
  * Linux: `~/.cache/video-previewer/`

  (override with `VIDEO_PREVIEWER_CACHE_DIR`).

## Tests

```bash
uv run pytest
```

Tests run headless (`QT_QPA_PLATFORM=offscreen`) and use a temporary cache
directory; the pipeline tests generate real test videos with ffmpeg and are
skipped automatically when ffmpeg is unavailable.

## Layout

```
settings.yml           # tunables (thumbnail, grid, playback, extensions)
src/video_previewer/
├── main.py            # entry point
├── app.py             # QApplication setup, dark palette
├── config.py          # settings.yml loader + defaults + paths
├── open_external.py   # open a video in the OS-default player
├── ui/                # main window, grid view, item delegate,
│                      #   folder sidebar, exit prompt
├── models/            # VideoItem, QAbstractListModel
├── media/             # ffprobe metadata, ffmpeg thumbnailer, shared
│                      #   player + timeline scrub bar overlay
├── cache/             # SQLite metadata DB + thumbnail cache policy
└── workers/           # scanner + bounded thumbnail queue (QThreadPool)
```

The playback backend (Qt Multimedia) is isolated in `media/player.py`; the
grid, cache, scanner, and thumbnail pipeline do not depend on it, so it can
be swapped for libmpv later if needed.
