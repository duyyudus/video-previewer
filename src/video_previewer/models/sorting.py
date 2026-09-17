"""Sort options + sort keys for the grid (name / date modified, both ways).

One definition shared by the model (which keeps its rows ordered) and the
toolbar menu (which offers the choices), so the labels, the persisted
setting values and the actual ordering can never drift apart.
"""

from __future__ import annotations

import re
from enum import StrEnum

from .video_item import VideoItem, normalize_path

_DIGITS = re.compile(r"(\d+)")


class SortKey(StrEnum):
    """What the grid orders by. Values are the persisted (QSettings) form."""

    NAME = "name"
    MODIFIED = "modified"

    @property
    def label(self) -> str:
        """Menu label."""
        return _KEY_LABELS[self]


class SortOrder(StrEnum):
    """Direction of a sort. Values are the persisted (QSettings) form."""

    ASC = "asc"
    DESC = "desc"

    @property
    def label(self) -> str:
        """Menu label."""
        return "Ascending" if self is SortOrder.ASC else "Descending"

    @property
    def arrow(self) -> str:
        """Small glyph for the toolbar button."""
        return "\u25b2" if self is SortOrder.ASC else "\u25bc"


_KEY_LABELS = {SortKey.NAME: "Name", SortKey.MODIFIED: "Date modified"}


def natural_key(name: str) -> tuple:
    """Case-insensitive, number-aware key: ``ep2`` sorts before ``ep10``.

    Each chunk becomes a ``(is_number, number, text)`` triple so integers
    are only ever compared with integers and text with text (a plain string
    compare would put ``ep10`` first).
    """
    chunks: list[tuple[int, int, str]] = []
    for chunk in _DIGITS.split(name.lower()):
        if not chunk:
            continue
        if chunk.isdigit():
            chunks.append((1, int(chunk), ""))
        else:
            chunks.append((0, 0, chunk))
    return tuple(chunks)


class _Descending:
    """Wraps a key so ``sorted()`` (which only ever uses ``<``) inverts it.

    Negating or reversing the raw key is not an option for strings/tuples,
    so the comparison itself is flipped.
    """

    __slots__ = ("key",)

    def __init__(self, key: object) -> None:
        self.key = key

    def __lt__(self, other: "_Descending") -> bool:
        return bool(other.key < self.key)  # type: ignore[operator]


#: One comparable value per item (the wrapper for descending sorts).
SortValue = tuple | _Descending


def item_sort_key(item: VideoItem, key: SortKey, order: SortOrder) -> SortValue:
    """Total-order sort value for *item*.

    The normalized path is always the tie-breaker: two files with the same
    name in different folders (or the same modification time) still get a
    stable, deterministic order instead of relying on arrival sequence.
    """
    primary: object = (
        natural_key(item.filename) if key is SortKey.NAME else item.modified
    )
    full = (primary, normalize_path(item.path))
    return full if order is SortOrder.ASC else _Descending(full)