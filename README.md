# video-previewer

Minimalistic youtube-like video previewer.

Browse a folder of local videos as a responsive thumbnail grid; hover a tile
to watch a short muted preview; sweep the pointer horizontally across the
tile to scrub through the video. Built for personal use — simple, fast, and
low on resources.

## How it works

* **Grid** — `QListView` + custom `QAbstractListModel` + custom delegate
  (model/view virtualization; only visible tiles are painted, so folders with
  thousands of videos stay responsive).
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
* **Scrubbing** — horizontal pointer position maps to a video timestamp
  (`x / width × duration`) and is applied as a throttled `setPosition`
  (max ~30 seeks/second). A thin timeline bar with a knob (bottom of the
  hovered tile) mirrors the pointer while you scrub and the real playback
  position while the preview loops.

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

* Supported formats: `.mp4 .mkv .mov .webm .avi .m4v`.
* The **Subfolders** checkbox toggles recursive scanning (remembered).
* The last opened folder is restored on next launch.
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
src/video_previewer/
├── main.py            # entry point
├── app.py             # QApplication setup, dark palette
├── config.py          # constants + paths
├── ui/                # main window, grid view, item delegate
├── models/            # VideoItem, QAbstractListModel
├── media/             # ffprobe metadata, ffmpeg thumbnailer, shared player
├── cache/             # SQLite metadata DB + thumbnail cache policy
└── workers/           # scanner + bounded thumbnail queue (QThreadPool)
```

The playback backend (Qt Multimedia) is isolated in `media/player.py`; the
grid, cache, scanner, and thumbnail pipeline do not depend on it, so it can
be swapped for libmpv later if needed.
