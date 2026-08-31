@echo off
rem Launch the video previewer (double-click friendly).
rem Requires: uv (https://docs.astral.sh/uv/) and ffmpeg/ffprobe on PATH.
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo [video-preview] uv not found on PATH.
    echo Install it from https://docs.astral.sh/uv/getting-started/installation/ and try again.
    pause
    exit /b 1
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
    echo [video-preview] warning: ffmpeg not found on PATH - thumbnails will be unavailable.
)

uv run video-preview
if errorlevel 1 (
    echo.
    echo [video-preview] the app exited with an error - see output above.
    pause
)
