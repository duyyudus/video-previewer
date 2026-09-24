"""Background video rotation (QRunnable).

Rotates a batch of files one after another (a single encode already keeps
the CPU/GPU busy), reporting overall progress weighted by duration. Each
finished file is installed immediately — overwriting the original, or after
moving it to ``.vpbackup/`` — so a cancellation keeps the files already done
and leaves the rest untouched.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import config
from ..media import rotator
from ..media.rotator import RotateDirection

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RotateReport:
    rotated: list[Path] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # "name: reason"
    cancelled: bool = False


class RotateSignals(QObject):
    progress = Signal(int, str)  # overall permille, status text
    file_done = Signal(str)      # path of a file now holding the rotated video
    finished = Signal(object)    # RotateReport


class RotateJob(QRunnable):
    def __init__(
        self,
        paths: list[Path],
        direction: RotateDirection,
        overwrite: bool,
        signals: RotateSignals,
        use_cuda: bool | None = None,
    ) -> None:
        super().__init__()
        self._paths = list(paths)
        self._direction = direction
        self._overwrite = overwrite
        self._signals = signals
        self._use_cuda = config.ROTATE_USE_CUDA if use_cuda is None else use_cuda
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

    @Slot()
    def run(self) -> None:
        report = RotateReport()
        try:
            self._rotate_all(report)
        except Exception as exc:  # noqa: BLE001 - never lose the finished signal
            log.exception("rotation job failed")
            report.failed.append(f"unexpected error: {exc}")
        finally:
            report.cancelled = self._cancel.is_set()
            self._signals.finished.emit(report)

    def _rotate_all(self, report: RotateReport) -> None:
        count = len(self._paths)
        self._signals.progress.emit(0, "Reading video information…")
        infos = [rotator.probe_source(p) for p in self._paths]
        known = [i.duration_s for i in infos if i and i.duration_s > 0]
        fallback = sum(known) / len(known) if known else 1.0
        weights = [i.duration_s if i and i.duration_s > 0 else fallback for i in infos]
        self._total = sum(weights) or 1.0
        self._done = 0.0
        for n, (src, info, weight) in enumerate(zip(self._paths, infos, weights), 1):
            if self._cancel.is_set():
                return
            self._weight = weight
            self._status = self._base = f"Rotating {n} of {count}: {src.name}"
            self._emit(0.0)
            if not src.exists():
                report.failed.append(f"{src.name}: file not found")
            elif info is None:
                report.failed.append(f"{src.name}: not a readable video")
            else:
                self._rotate_one(src, info, report)
            self._done += weight
        self._emit(0.0)

    def _emit(self, fraction: float) -> None:
        """Overall progress: finished files plus *fraction* of the current one."""
        permille = int(1000 * (self._done + self._weight * fraction) / self._total)
        self._signals.progress.emit(min(1000, permille), self._status)

    def _on_encoder(self, encoder: str) -> None:
        device = "NVIDIA GPU" if encoder.endswith("_nvenc") else "CPU"
        self._status = f"{self._base}\nEncoder: {encoder} ({device})"
        self._emit(0.0)

    def _rotate_one(
        self, src: Path, info: rotator.SourceInfo, report: RotateReport
    ) -> None:
        tmp = rotator.temp_output_path(src)
        try:
            encoder = rotator.rotate_file(
                src, tmp, self._direction, info,
                use_cuda=self._use_cuda,
                on_progress=self._emit,
                on_encoder=self._on_encoder,
                cancel_event=self._cancel,
            )
        except rotator.RotateCancelledError:
            return
        except rotator.RotateError as exc:
            report.failed.append(f"{src.name}: {exc}")
            return
        if self._cancel.is_set():
            # Cancelled between the encode and the swap: the original stays.
            tmp.unlink(missing_ok=True)
            return
        try:
            backup = rotator.install_rotated(src, tmp, self._overwrite)
        except OSError as exc:
            report.failed.append(f"{src.name}: {exc.strerror or exc}")
            return
        log.info(
            "rotated %s %s with %s%s", src, self._direction.value, encoder,
            f" (original kept at {backup})" if backup else "",
        )
        report.rotated.append(src)
        self._signals.file_done.emit(str(src))
