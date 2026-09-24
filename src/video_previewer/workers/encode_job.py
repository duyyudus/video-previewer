"""Shared base for sequential, cancellable ffmpeg batch jobs (QRunnable).

Processes a batch of files one after another (a single encode already keeps
the CPU/GPU busy), reporting overall progress weighted by duration.
Subclasses probe and process one file; each finished file is installed
immediately, so a cancellation keeps the files already done and leaves the
rest untouched.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Slot

log = logging.getLogger(__name__)


class EncodeJob(QRunnable):
    #: Status verb, e.g. "Rotating" -> "Rotating 2 of 5: name.mkv".
    VERB = "Processing"

    def __init__(self, paths: list[Path], signals: QObject, use_cuda: bool) -> None:
        super().__init__()
        self._paths = list(paths)
        self._signals = signals  # needs progress(int, str) + finished(object)
        self._use_cuda = use_cuda
        self._cancel = threading.Event()
        # Progress bookkeeping (worker thread only).
        self._total = 1.0
        self._done = 0.0
        self._weight = 0.0
        self._base = self._status = ""
        self.setAutoDelete(True)

    def cancel(self) -> None:
        """Thread-safe: stops the running ffmpeg and skips remaining files."""
        self._cancel.set()

    # -- subclass hooks ------------------------------------------------------------

    def _new_report(self) -> Any:
        """A report with ``failed: list[str]`` and ``cancelled: bool``."""
        raise NotImplementedError

    def _probe(self, path: Path) -> Any:
        """Per-file info (off the GUI thread); None = not a readable video."""
        raise NotImplementedError

    @staticmethod
    def _duration(info: Any) -> float:
        raise NotImplementedError

    def _process_one(self, src: Path, info: Any, report: Any) -> None:
        raise NotImplementedError

    # -- driver ---------------------------------------------------------------------

    @Slot()
    def run(self) -> None:
        report = self._new_report()
        try:
            self._process_all(report)
        except Exception as exc:  # noqa: BLE001 - never lose the finished signal
            log.exception("%s job failed", self.VERB.lower())
            report.failed.append(f"unexpected error: {exc}")
        finally:
            report.cancelled = self._cancel.is_set()
            self._signals.finished.emit(report)

    def _process_all(self, report: Any) -> None:
        count = len(self._paths)
        self._signals.progress.emit(0, "Reading video information…")
        infos = [self._probe(p) for p in self._paths]
        durations = [self._duration(i) if i is not None else 0.0 for i in infos]
        known = [d for d in durations if d > 0]
        fallback = sum(known) / len(known) if known else 1.0
        weights = [d if d > 0 else fallback for d in durations]
        self._total = sum(weights) or 1.0
        self._done = 0.0
        for n, (src, info, weight) in enumerate(zip(self._paths, infos, weights), 1):
            if self._cancel.is_set():
                return
            self._weight = weight
            self._status = self._base = f"{self.VERB} {n} of {count}: {src.name}"
            self._emit(0.0)
            if not src.exists():
                report.failed.append(f"{src.name}: file not found")
            elif info is None:
                report.failed.append(f"{src.name}: not a readable video")
            else:
                self._process_one(src, info, report)
            self._done += weight
        self._emit(0.0)

    def _emit(self, fraction: float) -> None:
        """Overall progress: finished files plus *fraction* of the current one."""
        permille = int(1000 * (self._done + self._weight * fraction) / self._total)
        self._signals.progress.emit(min(1000, permille), self._status)

    def _on_encoder(self, encoder: str) -> None:
        if encoder == "copy":
            detail = "stream copy (lossless)"
        else:
            device = "NVIDIA GPU" if encoder.endswith("_nvenc") else "CPU"
            detail = f"{encoder} ({device})"
        self._status = f"{self._base}\nEncoder: {detail}"
        self._emit(0.0)
