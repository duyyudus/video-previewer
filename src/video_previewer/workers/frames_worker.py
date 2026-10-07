"""Background extraction of video frames to image folders (QRunnable).

Extracts a batch of files one after another (see :mod:`.encode_job`). The
sources are only read; each video gets its own ``<name>_frames`` folder. A
cancellation keeps the folders already finished and removes the partial one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..media import frames, rotator
from ..media.frames import FrameOptions
from .encode_job import EncodeJob

log = logging.getLogger(__name__)


@dataclass(slots=True)
class FramesReport:
    extracted: list[tuple[Path, Path, int]] = field(default_factory=list)  # (src, folder, frames)
    failed: list[str] = field(default_factory=list)  # "name: reason"
    cancelled: bool = False


class FramesSignals(QObject):
    progress = Signal(int, str)  # overall permille, status text
    finished = Signal(object)    # FramesReport


class FramesJob(EncodeJob):
    VERB = "Extracting frames from video"

    def __init__(
        self,
        paths: list[Path],
        signals: FramesSignals,
        root: Path | None = None,
        options: FrameOptions | None = None,
    ) -> None:
        super().__init__(paths, signals, use_cuda=False)
        self._options = options or FrameOptions()
        self._root = root  # None: each folder goes next to its video

    def _new_report(self) -> FramesReport:
        return FramesReport()

    def _probe(self, path: Path) -> rotator.SourceInfo | None:
        return rotator.probe_source(path)

    @staticmethod
    def _duration(info: rotator.SourceInfo) -> float:
        return info.duration_s

    def _process_one(
        self, src: Path, info: rotator.SourceInfo, report: FramesReport
    ) -> None:
        folder = frames.frames_dir(src, self._root)
        step = self._options.step(info.fps)
        if step > 1:
            self._status = f"{self._base}\nKeeping 1 of every {step} frames"
            self._emit(0.0)
        try:
            count = frames.extract_frames(
                src, folder, info, self._options.fmt, step,
                on_progress=self._emit, cancel_event=self._cancel,
            )
        except frames.FramesCancelledError:
            return
        except frames.FramesError as exc:
            report.failed.append(f"{src.name}: {exc}")
            return
        log.info("extracted %d frame(s) of %s -> %s", count, src, folder)
        report.extracted.append((src, folder, count))
