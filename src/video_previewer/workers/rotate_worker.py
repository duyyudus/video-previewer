"""Background video rotation (QRunnable).

Rotates a batch of files one after another (see :mod:`.encode_job`). Each
finished file is installed immediately — overwriting the original, or after
moving it to ``.vpbackup/`` — so a cancellation keeps the files already done
and leaves the rest untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import config
from ..media import rotator
from ..media.rotator import RotateDirection
from .encode_job import EncodeJob

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


class RotateJob(EncodeJob):
    VERB = "Rotating"

    def __init__(
        self,
        paths: list[Path],
        direction: RotateDirection,
        overwrite: bool,
        signals: RotateSignals,
        use_cuda: bool | None = None,
    ) -> None:
        super().__init__(
            paths, signals, config.ROTATE_USE_CUDA if use_cuda is None else use_cuda
        )
        self._direction = direction
        self._overwrite = overwrite

    def _new_report(self) -> RotateReport:
        return RotateReport()

    def _probe(self, path: Path) -> rotator.SourceInfo | None:
        return rotator.probe_source(path)

    @staticmethod
    def _duration(info: rotator.SourceInfo) -> float:
        return info.duration_s

    def _process_one(
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
