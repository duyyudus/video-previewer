#!/usr/bin/env bash
# Launch the video previewer (macOS/Linux counterpart of run.bat).
# Requires: uv (https://docs.astral.sh/uv/) and ffmpeg/ffprobe on PATH.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v ffmpeg >/dev/null 2>&1; then
    echo "[video-preview] warning: ffmpeg not found on PATH - thumbnails will be unavailable."
fi

if command -v uv >/dev/null 2>&1; then
    # Creates .venv on first run; a fast no-op afterwards.
    uv sync
    exec uv run video-preview
fi

if [[ -x .venv/bin/video-preview ]]; then
    # No uv, but a prepared virtualenv: use it directly.
    exec .venv/bin/video-preview
fi

echo "[video-preview] uv not found on PATH and no .venv exists." >&2
echo "Install it from https://docs.astral.sh/uv/getting-started/installation/ and try again." >&2
exit 1