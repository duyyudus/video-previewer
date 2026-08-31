# Minimal Cross-Platform Video Preview App — Implementation Brief

## Goal

Build a minimal cross-platform desktop application for quickly browsing and previewing local video files.

Primary platforms:

* Windows
* Linux
* macOS

Preferred language:

* Python

Recommended UI framework:

* PySide6 / Qt 6

The application is for personal use and should prioritize simplicity, responsiveness, and low resource usage over extensive media-library features.

---

## Core UX

The main window should consist primarily of a responsive grid of video thumbnails, similar to a simplified YouTube browse page.

Basic interaction:

1. User selects a folder containing videos.
2. App scans supported video files.
3. Grid displays a thumbnail for each video.
4. Hovering a thumbnail starts muted video playback.
5. Moving the mouse horizontally across the thumbnail allows timeline scrubbing.
6. Leaving the thumbnail stops playback and restores the static thumbnail.

UI chrome should remain minimal.

---

## Core Features

### Folder Selection

Provide a minimal control for selecting a local folder.

Requirements:

* Native folder picker.
* Remember the most recently opened folder if practical.
* Scan supported video formats recursively only if configured; default can be current folder only.
* File scanning must not block the UI.

Initial supported formats should include:

* `.mp4`
* `.mkv`
* `.mov`
* `.webm`
* `.avi`
* `.m4v`

---

## Video Grid

Use Qt's model/view architecture rather than creating one full QWidget per video.

Recommended:

* `QListView`
* Custom `QAbstractListModel`
* Custom delegate for thumbnail rendering

Requirements:

* Responsive multi-column grid.
* Dynamically adapt column count to window width.
* Only render visible items.
* Support folders containing thousands of videos.
* Show filename below or over the thumbnail only if it does not make the interface visually noisy.

Avoid constructing thousands of heavyweight widget instances.

---

## Thumbnail Generation

Static thumbnails should be generated asynchronously.

Recommended tooling:

* `ffprobe` for metadata.
* `ffmpeg` for frame extraction.

Suggested thumbnail position:

* Around 10–20% into the video.
* Avoid always using frame zero because many videos begin with black frames or title cards.

Store:

* Thumbnail image.
* Duration.
* Video dimensions.
* File modification timestamp.
* File size if useful for cache validation.

Do not regenerate thumbnails unless the source video changed.

---

## Cache

Use a persistent local thumbnail cache.

Possible structure:

```text
cache/
├── thumbnails/
│   ├── <video-id>.jpg
│   └── ...
└── metadata.sqlite
```

A video ID can be derived from:

```text
absolute path + modification timestamp + file size
```

SQLite is recommended but optional for the first implementation.

Minimum cache requirements:

* Persistent between launches.
* Detect changed/deleted files.
* Avoid regenerating thumbnails unnecessarily.

---

## Hover Video Preview

The app must NOT instantiate a media player for every thumbnail.

Use one shared active preview player.

Recommended initial implementation:

* `QMediaPlayer`
* `QVideoSink` or appropriate Qt video output.
* Qt Multimedia FFmpeg backend.

Behavior:

```text
mouse enters thumbnail
        ↓
load corresponding video
        ↓
attach preview output to hovered tile
        ↓
play muted
```

When the pointer leaves:

```text
stop player
        ↓
detach preview
        ↓
restore cached static thumbnail
```

Only one preview decoder should normally be active at a time.

This is an important architectural requirement.

---

## Timeline Scrubbing

Horizontal pointer position over a thumbnail should correspond approximately to timeline position.

Formula:

```text
position = mouse_x / thumbnail_width
timestamp = position × video_duration
```

Example:

```text
left edge                right edge
0% -------------------------- 100%
0:00                         4:30
```

### Initial implementation

Use player seeking:

```python
player.setPosition(timestamp_ms)
```

This is sufficient for the first version.

Be aware that seeking speed depends on:

* Codec.
* Keyframe spacing.
* GOP size.
* Video resolution.

---

## Optional High-Performance Scrubbing

If direct seeking does not feel responsive enough, implement cached scrub frames.

Generate frames at regular intervals such as:

```text
0%
5%
10%
15%
...
95%
```

During rapid mouse movement:

* Display nearest cached preview frame.
* Avoid repeatedly seeking the video decoder.

When the mouse becomes stationary:

* Seek the actual media player to the selected position.
* Resume playback.

Desired interaction:

```text
normal state
    ↓
static thumbnail

hover
    ↓
video autoplay

mouse moves horizontally
    ↓
cached scrub frame

mouse stops
    ↓
seek actual video
    ↓
continue playback

mouse leaves
    ↓
static thumbnail
```

Treat this as a later optimization rather than a requirement for the initial MVP.

---

## Playback Behavior

Default preview behavior:

* Muted.
* Autoplay on hover.
* Stop immediately or after a very short grace period on mouse leave.
* Loop preview playback if the pointer remains over the video.
* Do not open multiple decoders.

Optional delay before autoplay:

```text
100–300 ms
```

This can prevent unnecessary decoder starts while quickly moving the pointer across the grid.

---

## Architecture

Suggested project structure:

```text
video-previewer/
├── pyproject.toml
├── src/
│   └── video_previewer/
│       ├── main.py
│       ├── app.py
│       ├── config.py
│       │
│       ├── ui/
│       │   ├── main_window.py
│       │   ├── video_grid.py
│       │   └── video_delegate.py
│       │
│       ├── models/
│       │   ├── video_item.py
│       │   └── video_model.py
│       │
│       ├── media/
│       │   ├── player.py
│       │   ├── metadata.py
│       │   └── thumbnailer.py
│       │
│       ├── cache/
│       │   ├── cache.py
│       │   └── database.py
│       │
│       └── workers/
│           ├── scanner.py
│           └── thumbnail_worker.py
│
└── tests/
```

Keep modules small and avoid unnecessary abstraction.

---

## Component Responsibilities

### `MainWindow`

Responsible for:

* Folder selection.
* Main grid.
* Basic application state.

Should contain very little media-processing logic.

### `VideoModel`

Represents scanned videos.

Each item should include approximately:

```python
VideoItem(
    path,
    filename,
    duration,
    width,
    height,
    thumbnail_path,
    modified_time,
)
```

### `Scanner`

Responsible for:

* Walking the selected directory.
* Filtering supported extensions.
* Reporting videos incrementally.
* Avoiding UI blocking.

### `Thumbnailer`

Responsible for:

* Running `ffmpeg`.
* Extracting representative frames.
* Writing thumbnails to cache.
* Updating the model asynchronously.

### `MetadataReader`

Use `ffprobe` to retrieve:

* Duration.
* Resolution.
* Codec information if required.

### `PreviewPlayer`

Maintain exactly one shared active player.

Responsible for:

* Loading selected video.
* Play/pause/stop.
* Seeking.
* Muting.
* Switching the video output between hovered items.

---

## Concurrency

Filesystem scanning and thumbnail generation must happen outside the GUI thread.

Possible approaches:

* `QThreadPool`
* `QRunnable`
* Qt signals/slots

Prefer Qt-native concurrency mechanisms over introducing asyncio unless there is a clear need.

The GUI thread should only handle:

* Rendering.
* Pointer events.
* Model updates.
* Player control.

---

## Performance Requirements

The design should comfortably support:

```text
100–1,000 videos:
no meaningful issue

1,000–10,000 videos:
should remain responsive with caching + model/view virtualization

10,000+ videos:
may require more careful incremental scanning and cache management
```

Important rules:

* Do not decode every video.
* Do not instantiate a `QMediaPlayer` per item.
* Do not generate every thumbnail synchronously before showing the grid.
* Show results incrementally as files are discovered.
* Cache expensive work.

---

## Media Backend

### Initial choice

Use:

```text
PySide6
+
Qt Multimedia
+
Qt's FFmpeg backend
```

This provides the simplest architecture.

### Possible fallback

If Qt Multimedia produces unacceptable:

* Seeking latency.
* Codec compatibility issues.
* Hardware-decoding issues.
* Playback startup latency.

Replace only the playback component with:

```text
libmpv
```

The grid, cache, scanner, and thumbnail architecture should remain independent of the chosen playback backend.

Do not start with libmpv unless testing shows Qt Multimedia is insufficient.

---

## Dependencies

Suggested initial dependencies:

```text
Python 3.12+
PySide6
FFmpeg
ffprobe
```

Package management:

```text
uv
```

Example project initialization:

```bash
uv init
uv add pyside6
```

FFmpeg can initially be treated as an external dependency.

Packaging FFmpeg with release builds can be addressed later.

---

## Packaging

Potential packaging tools:

* PyInstaller
* Nuitka

Packaging is not part of the initial MVP.

First ensure the application works correctly from the Python development environment on all target systems.

Later produce standalone builds for:

* Windows.
* Linux.
* macOS.

---

## MVP Scope

Implement the first version in this order.

### Phase 1 — Grid

Implement:

* Application window.
* Folder selection.
* Video scanning.
* Responsive thumbnail grid.
* Placeholder thumbnail while extraction is pending.

No playback yet.

### Phase 2 — Thumbnail Cache

Implement:

* FFmpeg thumbnail extraction.
* Asynchronous generation.
* Persistent cache.
* Metadata extraction.

### Phase 3 — Hover Playback

Implement:

* Shared `QMediaPlayer`.
* Muted autoplay.
* Attach preview to hovered tile.
* Stop/restore thumbnail when pointer leaves.

Validate resource usage carefully.

### Phase 4 — Timeline Scrubbing

Implement:

* Convert horizontal pointer position to video timestamp.
* Call shared player seek.
* Add seek throttling/debouncing.

Do not seek on every raw mouse event.

Approximately 30–60 seek updates per second maximum should be more than sufficient.

### Phase 5 — Polish

Consider:

* Hover autoplay delay.
* Preview looping.
* Last-folder persistence.
* Thumbnail sizing options.
* Sorting.
* Recursive folder toggle.
* Cache cleanup.

Keep all optional controls visually unobtrusive.

### Phase 6 — Advanced Scrubbing

Only if necessary:

* Generate scrub-frame strips.
* Cache preview frames.
* Display cached frames while pointer moves.
* Resume actual video playback when pointer stops.

---

## Non-Goals

Do not turn this into a full media-library application.

Avoid adding unless explicitly requested:

* Playlists.
* Tags.
* Ratings.
* Accounts.
* Cloud synchronization.
* Video editing.
* Media database management.
* Transcoding.
* Complex settings UI.
* Built-in file manager.
* Full video player controls.

Opening a video in an external/default player on click can be added later if useful.

---

## UI Direction

The application should remain deliberately minimal.

Main visual hierarchy:

```text
┌───────────────────────────────────────────────┐
│ Folder                                  [...] │
├───────────────────────────────────────────────┤
│                                               │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐       │
│  │         │  │         │  │         │       │
│  │  video  │  │  video  │  │  video  │       │
│  │         │  │         │  │         │       │
│  └─────────┘  └─────────┘  └─────────┘       │
│                                               │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐       │
│  │         │  │         │  │         │       │
│  └─────────┘  └─────────┘  └─────────┘       │
│                                               │
└───────────────────────────────────────────────┘
```

Avoid:

* Sidebars.
* Large toolbars.
* Multiple navigation layers.
* Persistent playback controls.

Most interaction should happen directly through the thumbnails.

---

## Key Engineering Constraints

These should be treated as hard architectural rules:

1. Use PySide6 / Qt 6.
2. Use Qt model/view for the thumbnail grid.
3. Thumbnail extraction must be asynchronous.
4. Expensive media metadata and thumbnails must be cached.
5. Only one hover-preview video decoder/player should normally exist.
6. Scrubbing must be throttled.
7. UI must remain responsive while scanning large folders.
8. Media backend should be isolated so Qt Multimedia can later be replaced by libmpv if necessary.
9. Keep the application minimal and avoid unnecessary features or abstractions.
10. Optimize only after measuring actual playback/seeking behavior.

---

## Initial Technical Target

The first usable version is complete when the user can:

```text
launch app
    ↓
select folder
    ↓
immediately see videos populating the grid
    ↓
see generated thumbnails
    ↓
hover any thumbnail
    ↓
watch muted preview
    ↓
move pointer horizontally to seek through video
    ↓
move pointer away
    ↓
return instantly to static thumbnail
```

This interaction is the core product. Everything else is secondary.
