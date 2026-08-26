from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .models import FileOperation, UndoEntry

APP_NAME: str = "seasonal-shift"


def get_state_dir() -> Path:
    xdg_state_home: str | None = os.getenv("XDG_STATE_HOME")

    if xdg_state_home:
        return Path(xdg_state_home) / APP_NAME

    return Path.home() / ".local" / "state" / APP_NAME


def get_default_undo_file() -> Path:
    state_dir: Path = get_state_dir()

    state_dir.mkdir(parents=True, exist_ok=True)

    timestamp: str = datetime.now().strftime("%Y%m%d-%H%M%S")

    return state_dir / f"undo-{timestamp}.json"


def find_latest_undo_file() -> Path | None:

    state_dir: Path = get_state_dir()

    if not state_dir.exists():
        return None

    undo_files = sorted(
        state_dir.glob("undo-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not undo_files:
        return None

    return undo_files[0]


def execute_operations(
    operations: list[FileOperation],
    undo_file: Path,
) -> None:

    undo_entries: list[UndoEntry] = []

    for op in operations:

        op.destination.parent.mkdir(parents=True, exist_ok=True)

        op.source.rename(op.destination)

        undo_entries.append(
            UndoEntry(
                source=op.destination,
                destination=op.source,
            )
        )

    undo_file.write_text(
        "[\n" + ",\n".join(e.model_dump_json(indent=2) for e in undo_entries) + "\n]"
    )
