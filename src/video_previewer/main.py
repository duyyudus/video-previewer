"""Entry point: ``uv run video-preview``."""

from __future__ import annotations

import logging
import sys

from .app import run


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(run())


if __name__ == "__main__":
    main()
