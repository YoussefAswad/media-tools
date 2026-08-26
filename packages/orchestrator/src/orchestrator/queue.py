"""Durable job queue over the shared SQLite db (``jobs`` table).

A job is enqueued by the webhook endpoint (return 200 fast) and consumed by the single worker
thread. Status lifecycle: ``queued → processing → done`` | back to ``queued`` on a retryable
failure until ``attempts >= max_attempts`` → ``dead``. The UI can ``cancel`` a queued job or
``retry`` a dead/failed one.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

QUEUED = "queued"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
DEAD = "dead"
CANCELED = "canceled"

ACTIVE = (QUEUED, PROCESSING)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    label        TEXT,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    current_step TEXT,
    error        TEXT,
    progress     REAL,
    progress_note TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class Job:
    id: str
    kind: str
    label: str | None
    payload: dict[str, Any]
    status: str
    attempts: int
    current_step: str | None
    error: str | None
    created_at: str
    updated_at: str
    progress: float | None = None
    progress_note: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Job:
        keys = row.keys()
        return cls(
            id=row["id"],
            kind=row["kind"],
            label=row["label"],
            payload=json.loads(row["payload"]),
            status=row["status"],
            attempts=row["attempts"],
            current_step=row["current_step"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            progress=row["progress"] if "progress" in keys else None,
            progress_note=row["progress_note"] if "progress_note" in keys else None,
        )


class JobQueue:
    def __init__(self, db: sqlite3.Connection, *, max_attempts: int = 3) -> None:
        self.db = db
        self.max_attempts = max_attempts
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the table's first creation (idempotent)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        if "progress" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN progress REAL")
        if "progress_note" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN progress_note TEXT")

    def enqueue(self, kind: str, payload: dict[str, Any], *, label: str | None = None) -> str:
        job_id = uuid.uuid4().hex
        now = _now()
        with self._lock:
            self.db.execute(
                "INSERT INTO jobs (id, kind, label, payload, status, attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (job_id, kind, label, json.dumps(payload), QUEUED, 0, now, now),
            )
            self.db.commit()
        return job_id

    def claim_next(self) -> Job | None:
        """Atomically move the oldest queued job to processing and return it."""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status=? ORDER BY created_at ASC LIMIT 1", (QUEUED,)
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET status=?, attempts=attempts+1, updated_at=? WHERE id=?",
                (PROCESSING, _now(), row["id"]),
            )
            self.db.commit()
            return Job.from_row(
                self.db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            )

    def set_step(self, job_id: str, step: str | None) -> None:
        with self._lock:
            # a new step resets any progress from the previous one
            self.db.execute(
                "UPDATE jobs SET current_step=?, progress=NULL, progress_note=NULL, updated_at=? "
                "WHERE id=?",
                (step, _now(), job_id),
            )
            self.db.commit()

    def set_progress(self, job_id: str, percent: float, note: str | None = None) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE jobs SET progress=?, progress_note=?, updated_at=? WHERE id=?",
                (percent, note, _now(), job_id),
            )
            self.db.commit()

    def mark_done(self, job_id: str) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE jobs SET status=?, current_step=NULL, error=NULL, progress=NULL, "
                "progress_note=NULL, updated_at=? WHERE id=?",
                (DONE, _now(), job_id),
            )
            self.db.commit()

    def mark_failed(self, job_id: str, error: str) -> str:
        """Record a failure; requeue if attempts remain, else dead-letter. Returns new status."""
        with self._lock:
            row = self.db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempts = row["attempts"] if row else self.max_attempts
            status = QUEUED if attempts < self.max_attempts else DEAD
            self.db.execute(
                "UPDATE jobs SET status=?, current_step=NULL, error=?, progress=NULL, "
                "progress_note=NULL, updated_at=? WHERE id=?",
                (status, error, _now(), job_id),
            )
            self.db.commit()
            return status

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE id=? AND status=?",
                (CANCELED, _now(), job_id, QUEUED),
            )
            self.db.commit()
            return cur.rowcount > 0

    def retry(self, job_id: str) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE jobs SET status=?, attempts=0, error=NULL, updated_at=? "
                "WHERE id=? AND status IN (?,?,?)",
                (QUEUED, _now(), job_id, DEAD, FAILED, CANCELED),
            )
            self.db.commit()
            return cur.rowcount > 0

    def get(self, job_id: str) -> Job | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        q: str | None = None,
        limit: int = 100,
    ) -> list[Job]:
        where: list[str] = []
        params: list = []
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if q:
            where.append("(label LIKE ? OR payload LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self.db.execute(
            f"SELECT * FROM jobs{clause} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def clear_dead(self) -> int:
        """Delete finished-but-not-successful jobs (dead / failed / canceled)."""
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM jobs WHERE status IN (?,?,?)", (DEAD, FAILED, CANCELED)
            )
            self.db.commit()
            return cur.rowcount

    def delete(self, job_id: str) -> bool:
        """Remove a single job from history. Refuses to delete a job still in flight."""
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM jobs WHERE id=? AND status != ?", (job_id, PROCESSING)
            )
            self.db.commit()
            return cur.rowcount > 0

    def requeue_stuck(self) -> int:
        """On startup, move any ``processing`` jobs (from a crash) back to ``queued``."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE jobs SET status=?, updated_at=? WHERE status=?",
                (QUEUED, _now(), PROCESSING),
            )
            self.db.commit()
            return cur.rowcount
