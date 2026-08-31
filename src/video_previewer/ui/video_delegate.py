"""Custom delegate: paints one video tile (thumbnail + filename).

Only visible items are painted (QListView virtualizes), and decoded
thumbnails are kept in a small LRU pixmap cache so scrolling never hits
disk more than once per video.
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen, QPolygon, QPixmap
from PySide6.QtWidgets import QStyledItemDelegate

from .. import config
from ..models.video_model import VideoModel

_TILE_BG = QColor(28, 28, 34)
_TILE_BORDER = QColor(60, 60, 70)
_PLACEHOLDER_BG = QColor(38, 38, 46)
_PLACEHOLDER_GLYPH = QColor(110, 110, 125)
_NAME_COLOR = QColor(225, 225, 232)
_NAME_COLOR_DIM = QColor(150, 150, 160)

_PIXMAP_CACHE_LIMIT = 256


class VideoDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pixmaps: OrderedDict[str, QPixmap] = OrderedDict()

    # -- sizing ---------------------------------------------------------------

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        w = option.rect.width() or config.CELL_WIDTH
        return QSize(w, cell_height(w))

    # -- painting ---------------------------------------------------------------

    def paint(self, painter: QPainter, option, index) -> None:
        # The cell pitch includes the grid gutter (IconMode ignores
        # setSpacing); draw the card inset so the gutter stays visible.
        item_rect = QRect(option.rect).adjusted(
            0, 0, -config.GRID_SPACING, -config.GRID_SPACING
        )
        img_rect = QRect(
            item_rect.left(),
            item_rect.top(),
            item_rect.width(),
            int(item_rect.width() * config.CELL_ASPECT),
        )
        name_rect = item_rect.adjusted(2, img_rect.height() + 2, -2, 0)

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # Tile background
        painter.setPen(QPen(_TILE_BORDER, 1))
        painter.setBrush(_TILE_BG)
        painter.drawRoundedRect(item_rect, 8, 8)

        thumb_path = index.data(VideoModel.ThumbnailPathRole)
        ready = bool(index.data(VideoModel.ThumbReadyRole))
        if ready and thumb_path:
            pix = self._load_pixmap(str(thumb_path))
            if not pix.isNull():
                painter.setClipRect(img_rect)
                painter.drawPixmap(img_rect, pix, cover_source(pix, img_rect))
                painter.setClipping(False)
        else:
            self._paint_placeholder(painter, img_rect)

        # Filename
        filename = str(index.data(VideoModel.FilenameRole) or "")
        font = option.font
        font.setPointSizeF(font.pointSizeF() - 0.5)
        painter.setFont(font)
        fm = QFontMetrics(font)
        text = fm.elidedText(filename, Qt.TextElideMode.ElideMiddle, name_rect.width())
        painter.setPen(_NAME_COLOR if ready else _NAME_COLOR_DIM)
        painter.drawText(
            name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text
        )

    # -- helpers ------------------------------------------------------------------

    def _paint_placeholder(self, painter: QPainter, img_rect: QRect) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_PLACEHOLDER_BG)
        painter.drawRect(img_rect)
        # centered "play" glyph
        side = min(img_rect.width(), img_rect.height()) // 5
        cx, cy = img_rect.center().x(), img_rect.center().y()
        painter.setBrush(_PLACEHOLDER_GLYPH)
        tri = QPolygon(
            [
                QPoint(cx - side // 2, cy - side // 2),
                QPoint(cx - side // 2, cy + side // 2),
                QPoint(cx + side // 2, cy),
            ]
        )
        painter.drawPolygon(tri)

    def _load_pixmap(self, path: str) -> QPixmap:
        pix = self._pixmaps.get(path)
        if pix is not None:
            self._pixmaps.move_to_end(path)
            return pix
        pix = QPixmap()
        pix.load(path)
        if pix.isNull():
            return pix  # don't cache failures
        self._pixmaps[path] = pix
        self._pixmaps.move_to_end(path)
        while len(self._pixmaps) > _PIXMAP_CACHE_LIMIT:
            self._pixmaps.popitem(last=False)
        return pix

    def clear_pixmap_cache(self) -> None:
        self._pixmaps.clear()


def cell_height(width: int) -> int:
    return int(width * config.CELL_ASPECT) + config.FILENAME_ROW


def cover_source(pix: QPixmap, dst: QRect) -> QRect:
    """Source rect of *pix* that covers *dst* 1:1 after scaling (center crop)."""
    sw, sh = pix.width(), pix.height()
    dw, dh = dst.width(), dst.height()
    if sw <= 0 or sh <= 0 or dw <= 0 or dh <= 0:
        return QRect()
    scale = max(dw / sw, dh / sh)
    crop_w = max(1, int(dw / scale))
    crop_h = max(1, int(dh / scale))
    x = (sw - crop_w) // 2
    y = (sh - crop_h) // 2
    return QRect(x, y, crop_w, crop_h)
