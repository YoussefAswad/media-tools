"""Audit log: append-only JSONL + a queryable SQLite mirror.

Every action the orchestrator takes is recorded here (and, via the notifier, surfaced to ntfy).
The JSONL is the durable source of truth; the SQLite ``actions`` table is an index for the web
UI and for dedup. The same SQLite file (opened via :func:`mediacore.db.open_db`) also holds the
orchestrator's ``jobs`` queue, so the UI can join jobs to their actions.
"""

from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

from mediacore.db import add_columns

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
    error      TEXT,
    duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_job  ON actions(job_id);
CREATE INDEX IF NOT EXISTS idx_actions_path ON actions(path);
CREATE INDEX IF NOT EXISTS idx_actions_ts   ON actions(ts);
"""

#: columns added after the table's first release — see :func:`mediacore.db.add_columns`.
_ACTIONS_ADDED = {"duration_ms": "INTEGER"}

# result values
OK = "ok"
SKIP = "skip"
FAIL = "fail"


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


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
    duration_ms: int | None = None
    ts: str = field(default_factory=_now)


class Audit:
    def __init__(self, db: sqlite3.Connection, state_dir: Path) -> None:
        self.db = db
        self.state_dir = Path(state_dir)
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_ACTIONS_SCHEMA)
            add_columns(self.db, "actions", _ACTIONS_ADDED)
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
                    before, after, undo_ref, error, duration_ms)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                    rec.duration_ms,
                ),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def last_done_at(self, path: str, action: str, mtime: float | None) -> str | None:
        """Timestamp of the most recent successful ``action`` on this exact file (by path+mtime),
        or None. Matching on mtime is what makes it file-identity rather than path-identity: a
        file replaced since is a different question.

        This answers "when was it done", for display — the Library tab's ``pruned_at``. It is
        deliberately *not* used to decide whether to do work: it cannot see a rename, and a
        cached yes/no overrides a live decision the moment the policy changes. What is
        outstanding is decided by ``policy.decide`` from the file's current contents."""
        if mtime is None:
            return None
        row = self.db.execute(
            "SELECT ts FROM actions WHERE path=? AND action=? AND result=? AND mtime=? "
            "ORDER BY ts DESC LIMIT 1",
            (path, action, OK, mtime),
        ).fetchone()
        return row["ts"] if row else None

    def recent(
        self,
        *,
        limit: int = 100,
        results: list[str] | None = None,
        q: str | None = None,
        tool: str | None = None,
        app: str | None = None,
    ) -> list[dict]:
        where: list[str] = []
        params: list = []
        if results:
            where.append(f"result IN ({','.join('?' for _ in results)})")
            params.extend(results)
        if tool:
            where.append("tool = ?")
            params.append(tool)
        if app:
            where.append("app = ?")
            params.append(app)
        if q:
            where.append("(path LIKE ? OR after LIKE ? OR error LIKE ? OR app LIKE ?)")
            params.extend([f"%{q}%"] * 4)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.db.execute(
            f"SELECT * FROM actions{clause} ORDER BY id DESC LIMIT ?", (*params, limit)
        ).fetchall()
        return [dict(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT result, COUNT(*) c FROM actions GROUP BY result").fetchall()
        return {r["result"]: r["c"] for r in rows}

    def for_job(self, job_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM actions WHERE job_id=? ORDER BY id ASC", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_old(self, days: int) -> int:
        """Delete SQLite ``actions`` rows older than ``days``. The JSONL files are the durable
        source of truth and are left untouched — this only trims the queryable mirror. Returns
        the number of rows removed; a non-positive ``days`` disables pruning."""
        if days <= 0:
            return 0
        cutoff = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            cur = self.db.execute("DELETE FROM actions WHERE ts<=?", (cutoff,))
            self.db.commit()
            return cur.rowcount
