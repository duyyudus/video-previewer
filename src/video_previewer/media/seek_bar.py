"""Thin seek/progress overlay shown at the bottom of the active preview tile.

One instance exists for the whole app (child of the shared video widget),
so scrubbing is never "blind": the fill mirrors the pointer while dragging
and the real playback position otherwise.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

WIDGET_HEIGHT = 10  # total overlay height (room for the knob to protrude)
BAR_HEIGHT = 5  # the actual track height, vertically centered
KNOB_RADIUS = 4.5

_TRACK = QColor(0, 0, 0, 120)
_FILL = QColor(240, 240, 245, 230)
_KNOB = QColor(255, 255, 255, 255)


class SeekBarOverlay(QWidget):
    """Visually pinned to the bottom edge of its parent (the video widget)."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        # Canvas-like widget: no palette background, only the painted bar,
        # so the video frames show through everywhere else.
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFixedHeight(WIDGET_HEIGHT)
        self._progress = 0.0
        # Follow the video widget when it is moved/resized between tiles.
        parent.installEventFilter(self)

    # -- API ----------------------------------------------------------------

    @property
    def progress(self) -> float:
        return self._progress

    def set_progress(self, value: float) -> None:
        """Set the 0..1 fill position; repainting only on real changes."""
        p = max(0.0, min(1.0, value))
        if abs(p - self._progress) < 0.0005:
            return
        self._progress = p
        self.update()

    # -- internals ------------------------------------------------------------

    def eventFilter(self, obj, event) -> bool:
        parent = self.parentWidget()
        if obj is parent and event.type() == QEvent.Type.Resize:
            h = parent.height()
            if h > self.height():
                self.setGeometry(0, h - self.height(), parent.width(), self.height())
        return super().eventFilter(obj, event)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        w, h = self.width(), self.height()
        if w <= 0:
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        bar_y = (h - BAR_HEIGHT) / 2.0

        # Track
        painter.setBrush(_TRACK)
        painter.drawRoundedRect(
            QRectF(0, bar_y, w, BAR_HEIGHT), BAR_HEIGHT / 2, BAR_HEIGHT / 2
        )

        # Fill
        if self._progress > 0:
            fw = min(w, max(BAR_HEIGHT, int(w * self._progress)))
            painter.setBrush(_FILL)
            painter.drawRoundedRect(
                QRectF(0, bar_y, fw, BAR_HEIGHT), BAR_HEIGHT / 2, BAR_HEIGHT / 2
            )

        # Knob
        painter.setBrush(_KNOB)
        painter.drawEllipse(
            QPointF(w * self._progress, h / 2.0), KNOB_RADIUS, KNOB_RADIUS
        )
