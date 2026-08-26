"""Audit log: append-only JSONL + a queryable SQLite mirror.

Every action the orchestrator takes is recorded here (and, via the notifier, surfaced to ntfy).
The JSONL is the durable source of truth; the SQLite ``actions`` table is an index for the web
UI and for dedup. The same SQLite file (opened via :func:`open_db`) also holds the orchestrator's
``jobs`` queue, so the UI can join jobs to their actions.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

DB_NAME = "orchestrator.db"

_ACTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    job_id     TEXT,
    app        TEXT,
    event_type TEXT,
    tool       TEXT NOT NULL,
    action     TEXT NOT NULL,
    path       TEXT,
    mtime      REAL,
    result     TEXT NOT NULL,
    before     TEXT,
    after      TEXT,
    undo_ref   TEXT,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_job  ON actions(job_id);
CREATE INDEX IF NOT EXISTS idx_actions_path ON actions(path);
CREATE INDEX IF NOT EXISTS idx_actions_ts   ON actions(ts);
"""

# result values
OK = "ok"
SKIP = "skip"
FAIL = "fail"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def open_db(state_dir: Path) -> sqlite3.Connection:
    """Open (creating parents) the shared SQLite db in WAL mode, thread-safe for the worker."""
    state_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(state_dir / DB_NAME), check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=5000")
    db.execute("PRAGMA foreign_keys=ON")
    return db


@dataclass
class ActionRecord:
    tool: str
    action: str
    result: str
    app: str | None = None
    event_type: str | None = None
    path: str | None = None
    mtime: float | None = None
    before: dict | None = None
    after: dict | None = None
    undo_ref: str | None = None
    error: str | None = None
    job_id: str | None = None
    ts: str = field(default_factory=_now)


class Audit:
    def __init__(self, db: sqlite3.Connection, state_dir: Path) -> None:
        self.db = db
        self.state_dir = Path(state_dir)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_ACTIONS_SCHEMA)
            self.db.commit()

    def _jsonl_path(self) -> Path:
        month = _dt.datetime.now(_dt.UTC).strftime("%Y%m")
        return self.state_dir / "audit" / f"actions-{month}.jsonl"

    def record(self, rec: ActionRecord) -> int:
        """Append to JSONL and insert into SQLite. Returns the SQLite row id."""
        with self._lock:
            p = self._jsonl_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(rec), default=str) + "\n")
            cur = self.db.execute(
                """INSERT INTO actions
                   (ts, job_id, app, event_type, tool, action, path, mtime, result,
                    before, after, undo_ref, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    rec.ts,
                    rec.job_id,
                    rec.app,
                    rec.event_type,
                    rec.tool,
                    rec.action,
                    rec.path,
                    rec.mtime,
                    rec.result,
                    json.dumps(rec.before) if rec.before is not None else None,
                    json.dumps(rec.after) if rec.after is not None else None,
                    rec.undo_ref,
                    rec.error,
                ),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def already_done(self, path: str, action: str, mtime: float | None) -> bool:
        """True if this exact file (by path+mtime) already had this action succeed."""
        if mtime is None:
            return False
        row = self.db.execute(
            "SELECT 1 FROM actions WHERE path=? AND action=? AND result=? AND mtime=? LIMIT 1",
            (path, action, OK, mtime),
        ).fetchone()
        return row is not None

    def recent(self, *, limit: int = 100, results: list[str] | None = None) -> list[dict]:
        if results:
            placeholders = ",".join("?" for _ in results)
            rows = self.db.execute(
                f"SELECT * FROM actions WHERE result IN ({placeholders}) "
                "ORDER BY id DESC LIMIT ?",
                (*results, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM actions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def for_job(self, job_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM actions WHERE job_id=? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]
