"""Background aspect-ratio change (QRunnable).

Stretches/squashes a batch of files one after another (see
:mod:`.encode_job`). Each finished file is installed immediately — overwriting
the original, or after moving it to ``.vpbackup/`` — so a cancellation keeps
the files already done and leaves the rest untouched. Videos that already
have the target ratio are skipped (known only after probing, off the GUI
thread).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import config
from ..media import aspect, rotator
from ..media.aspect import AspectRatio
from .encode_job import EncodeJob

log = logging.getLogger(__name__)


@dataclass(slots=True)
class AspectReport:
    ratio: str = ""  # target label, e.g. "16:9"
    changed: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # names already at the ratio
    failed: list[str] = field(default_factory=list)  # "name: reason"
    cancelled: bool = False


class AspectSignals(QObject):
    progress = Signal(int, str)  # overall permille, status text
    file_done = Signal(str)      # path of a file now holding the new ratio
    finished = Signal(object)    # AspectReport


class AspectJob(EncodeJob):
    VERB = "Changing ratio"

    def __init__(
        self,
        paths: list[Path],
        ratio: AspectRatio,
        overwrite: bool,
        signals: AspectSignals,
        use_cuda: bool | None = None,
    ) -> None:
        super().__init__(
            paths, signals, config.ASPECT_USE_CUDA if use_cuda is None else use_cuda
        )
        self._ratio = ratio
        self._overwrite = overwrite

    def _new_report(self) -> AspectReport:
        return AspectReport(ratio=self._ratio.label)

    def _probe(self, path: Path) -> rotator.SourceInfo | None:
        return rotator.probe_source(path)

    @staticmethod
    def _duration(info: rotator.SourceInfo) -> float:
        return info.duration_s

    def _process_one(
        self, src: Path, info: rotator.SourceInfo, report: AspectReport
    ) -> None:
        if aspect.already_at(info, self._ratio):
            report.skipped.append(src.name)
            return
        tmp = aspect.temp_output_path(src)
        try:
            encoder = aspect.reshape_file(
                src, tmp, self._ratio, info,
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
            "changed ratio of %s to %s with %s%s", src, self._ratio.label, encoder,
            f" (original kept at {backup})" if backup else "",
        )
        report.changed.append(src)
        self._signals.file_done.emit(str(src))
