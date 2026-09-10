"""The shared SQLite connection, and the one way tables gain columns.

One file (``/state/orchestrator.db``, WAL) holds every table the service keeps: jobs, actions,
downloads, quota periods, metrics, alert state/events, the library index, and a small meta key/value
store. They share a single connection, which is why :class:`_SerializedConnection` exists — see
its docstring, and never open a raw ``sqlite3.connect`` alongside it.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path

DB_NAME = "orchestrator.db"


class _BufferedResult:
    """A cursor-compatible view whose rows were already fetched (under the lock) by
    :meth:`_SerializedConnection.execute`. Fetching/iterating never touches SQLite again, so
    the connection is only ever driven by one thread at a time."""

    __slots__ = ("_i", "_rows", "lastrowid", "rowcount")

    def __init__(self, rows: list, lastrowid: int | None, rowcount: int) -> None:
        self._rows = rows
        self._i = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def fetchone(self):
        if self._i >= len(self._rows):
            return None
        row = self._rows[self._i]
        self._i += 1
        return row

    def fetchall(self) -> list:
        rows = self._rows[self._i :]
        self._i = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _SerializedConnection(sqlite3.Connection):
    """A shared connection safe for concurrent use across threads (the web threadpool + the
    worker). Python's ``sqlite3`` protects the C structures, but two threads stepping the *same*
    connection can still interleave and hand back a truncated/``None`` result. We serialize every
    statement — and eagerly buffer each query's rows — behind one reentrant lock so callers keep
    the ``execute(...).fetchone()`` idiom unchanged."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()

    def execute(self, sql, parameters=(), /):  # type: ignore[override]
        with self._lock:
            cur = super().execute(sql, parameters)
            rows = cur.fetchall() if cur.description is not None else []
            return _BufferedResult(rows, cur.lastrowid, cur.rowcount)

    def executescript(self, sql_script, /):  # type: ignore[override]
        with self._lock:
            return super().executescript(sql_script)

    def commit(self) -> None:
        with self._lock:
            super().commit()


def open_db(state_dir: Path) -> sqlite3.Connection:
    """Open (creating parents) the shared SQLite db in WAL mode, thread-safe for the worker."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(
        str(state_dir / DB_NAME), check_same_thread=False, factory=_SerializedConnection
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


def add_columns(
    db: sqlite3.Connection, table: str, columns: Mapping[str, str]
) -> list[str]:
    """Add whichever of ``columns`` the table doesn't have yet. Idempotent; returns what it added.

    This is how every table in this db migrates. Each one used to hand-roll the same
    ``PRAGMA table_info`` loop plus a stack of guarded ``ALTER TABLE ADD COLUMN`` statements —
    fine at two tables, duplicated infrastructure at nine, and the failure mode of a forgotten
    guard only ever shows up on somebody else's older database.

    ``columns`` maps a column name to its SQL declaration (``"progress": "REAL"``,
    ``"revives": "INTEGER NOT NULL DEFAULT 0"``). SQLite's ``ADD COLUMN`` requires any
    ``NOT NULL`` column to carry a default, which is why those declarations include one.

    ``table`` and the declarations are interpolated into SQL, so they must be code constants —
    never anything derived from config or a request. Callers pass module-level literals.
    """
    have = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    added: list[str] = []
    for name, decl in columns.items():
        if name not in have:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            added.append(name)
    return added
