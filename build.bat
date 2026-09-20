@echo off
rem Build a console-free Windows application bundle with PyInstaller.
setlocal
cd /d "%~dp0"

set "PYTHON=.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
    echo [video-preview] project environment not found.
    echo Run "uv sync" first, then try again.
    pause
    exit /b 1
)

"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --clean ^
    --windowed ^
    --icon assets\video-previewer.ico ^
    --add-data "assets\video-previewer.png;assets" ^
    --name video-previewer ^
    --paths src ^
    scripts\pyinstaller_entry.py
if errorlevel 1 (
    echo.
    echo [video-preview] build failed - see output above.
    pause
    exit /b 1
)

copy /Y settings.yml dist\video-previewer\settings.yml >nul
if errorlevel 1 (
    echo.
    echo [video-preview] build succeeded, but settings.yml could not be copied.
    pause
    exit /b 1
)

echo.
echo [video-preview] build complete:
echo %CD%\dist\video-previewer\video-previewer.exe
pause
