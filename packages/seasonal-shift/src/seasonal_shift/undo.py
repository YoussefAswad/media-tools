from __future__ import annotations

import json
from pathlib import Path

from .models import UndoEntry


def run_undo(undo_file: Path) -> None:

    data = json.loads(undo_file.read_text())  # pyright: ignore[reportAny]

    entries: list[UndoEntry] = [
        UndoEntry.model_validate(e) for e in data  # pyright: ignore[reportAny]
    ]

    # reverse order for safety
    for entry in reversed(entries):

        if not entry.source.exists():
            continue

        # recreate directory if cleanup removed it
        entry.destination.parent.mkdir(parents=True, exist_ok=True)

        entry.source.rename(entry.destination)
