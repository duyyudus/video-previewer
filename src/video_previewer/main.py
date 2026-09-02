"""Entry point: ``uv run video-preview``."""

from __future__ import annotations

import logging
import os
import sys

# Qt reads this exactly once at startup (C++ side, full rule grammar):
# silence the FFmpeg backend's Qt-side info/debug chatter, keep warnings
# and criticals. Must be set before any PySide6 import.
os.environ.setdefault(
    "QT_LOGGING_RULES",
    "qt.multimedia.ffmpeg=false;"
    "qt.multimedia.ffmpeg.warning=true;"
    "qt.multimedia.ffmpeg.critical=true",
)

from . import config  # noqa: E402
from .app import run  # noqa: E402


def _redirect_c_stderr() -> None:
    """Point C-level stderr (fd 2) at ``<cache_dir>/console.log``.

    The FFmpeg library statically linked into Qt writes its demuxer/decoder
    banners ("Input #0, ...") directly to the C runtime's stderr, bypassing
    Qt's logging system and any category filters. Redirecting fd 2 keeps the
    user's console clean while preserving that output (and any native crash
    text) in a log file. Python's own logging is pointed at stdout by
    ``main()``, so app log lines stay visible in the console.

    Failures here never break startup: the original stderr is kept.
    """
    try:
        log_path = config.app_cache_dir() / "console.log"
        if os.name == "nt":
            import ctypes
            import msvcrt

            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=True)
            k32 = ctypes.windll.kernel32
            # Explicit prototype: without restype=HANDLE the default c_int
            # return truncates the 64-bit pointer, and argtypes keep the
            # DWORD/HANDLE parameters correctly sized too.
            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [
                ctypes.c_wchar_p,  # lpFileName
                ctypes.c_uint32,  # dwDesiredAccess
                ctypes.c_uint32,  # dwShareMode
                ctypes.c_void_p,  # lpSecurityAttributes
                ctypes.c_uint32,  # dwCreationDisposition
                ctypes.c_uint32,  # dwFlagsAndAttributes
                ctypes.c_void_p,  # hTemplateFile
            ]
            handle = k32.CreateFileW(
                str(log_path),
                0x40000000,  # GENERIC_WRITE
                0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE
                None,
                3,  # OPEN_EXISTING
                0x02000000,  # FILE_FLAG_BACKUP_SEMANTICS
                None,
            )
            # restype c_void_p: NULL arrives as None, INVALID_HANDLE_VALUE
            # (-1) as the platform-width integer.
            if handle is None or handle == ctypes.c_void_p(-1).value:
                return
            fd = msvcrt.open_osfhandle(handle, os.O_APPEND | os.O_WRONLY)
            try:
                os.dup2(fd, 2)
            finally:
                # The CRT fd is now redundant (fd 2 refers to the same OS
                # handle, which _close deliberately leaves open); drop it so
                # the descriptor table stays clean.
                os.close(fd)
        elif os.name == "posix":
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "ab", buffering=0)
            os.dup2(log_file.fileno(), 2)
    except Exception:  # noqa: BLE001 - never break startup over logging setup
        pass


def main() -> None:
    _redirect_c_stderr()
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,  # stderr may be redirected to the log file
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(run())


if __name__ == "__main__":
    main()
