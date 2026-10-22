from __future__ import annotations

from pathlib import Path

from mediacore.renumber.models import FileOperation, UndoEntry


def execute_operations(operations: list[FileOperation], undo_file: Path) -> None:
    """Apply the planned renames, writing an undo journal as it goes.

    The undo file is written by the caller's chosen path (the orchestrator keeps one per item
    under its state dir) — there is no implicit XDG location, because the only caller is a
    service with its own state directory.

    Refuses, before moving anything, when a destination is already taken: ``Path.rename``
    silently replaces it on POSIX, and the file it would replace is another episode's import.
    """
    for op in operations:
        if op.destination.exists() and op.destination != op.source:
            raise FileExistsError(f"destination already exists: {op.destination}")

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
