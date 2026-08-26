from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def remove_empty_dirs(root: Path) -> list[Path]:
    """
    Remove empty directories recursively under root.

    Returns list of removed directories.
    """

    removed: list[Path] = []

    # walk deepest directories first
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):

        if path.is_dir():

            try:
                next(path.iterdir())
            except StopIteration:
                path.rmdir()
                removed.append(path)

    return removed


def cleanup_shows(show_paths: Iterable[Path]) -> list[Path]:

    removed: list[Path] = []

    for path in show_paths:
        removed.extend(remove_empty_dirs(path))

    return removed
