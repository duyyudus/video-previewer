"""Background conversion of videos to MP4 (QRunnable).

Converts a batch of files one after another (see :mod:`.encode_job`). Each
finished ``name.mp4`` is installed next to its source immediately and the
original deleted — or moved to ``.vpbackup/`` — so a cancellation keeps the
files already done and leaves the rest untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from .. import config
from ..media import converter
from .encode_job import EncodeJob

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ConvertReport:
    converted: list[tuple[Path, Path]] = field(default_factory=list)  # (src, mp4)
    failed: list[str] = field(default_factory=list)  # "name: reason"
    cancelled: bool = False


class ConvertSignals(QObject):
    progress = Signal(int, str)   # overall permille, status text
    file_done = Signal(str, str)  # source path (now gone), new .mp4 path
    finished = Signal(object)     # ConvertReport


class ConvertJob(EncodeJob):
    VERB = "Converting"

    def __init__(
        self,
        paths: list[Path],
        overwrite: bool,
        signals: ConvertSignals,
        use_cuda: bool | None = None,
    ) -> None:
        super().__init__(
            paths, signals, config.CONVERT_USE_CUDA if use_cuda is None else use_cuda
        )
        self._overwrite = overwrite

    def _new_report(self) -> ConvertReport:
        return ConvertReport()

    def _probe(self, path: Path) -> converter.ConvertPlan | None:
        return converter.probe_plan(path)

    @staticmethod
    def _duration(plan: converter.ConvertPlan) -> float:
        return plan.info.duration_s

    def _process_one(
        self, src: Path, plan: converter.ConvertPlan, report: ConvertReport
    ) -> None:
        tmp = converter.temp_output_path(src)
        try:
            encoder = converter.convert_file(
                src, tmp, plan,
                use_cuda=self._use_cuda,
                on_progress=self._emit,
                on_encoder=self._on_encoder,
                cancel_event=self._cancel,
            )
        except converter.ConvertCancelledError:
            return
        except converter.ConvertError as exc:
            report.failed.append(f"{src.name}: {exc}")
            return
        if self._cancel.is_set():
            # Cancelled between the encode and the install: the original stays.
            tmp.unlink(missing_ok=True)
            return
        dest = converter.output_path(src)
        try:
            backup = converter.install_converted(src, tmp, dest, self._overwrite)
        except converter.OriginalKeptError as exc:
            report.failed.append(
                f"{src.name}: converted to {dest.name}, but the original could "
                f"not be deleted ({exc})"
            )
            backup = None  # both files exist: still show the new one
        except OSError as exc:
            report.failed.append(f"{src.name}: {exc.strerror or exc}")
            return
        log.info(
            "converted %s -> %s with %s%s", src, dest.name, encoder,
            f" (original kept at {backup})" if backup else "",
        )
        report.converted.append((src, dest))
        self._signals.file_done.emit(str(src), str(dest))
