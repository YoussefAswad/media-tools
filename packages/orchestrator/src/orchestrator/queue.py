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
from datetime import UTC, datetime, timedelta
from typing import Any

# Default per-attempt requeue backoff (seconds), indexed by attempt and clamped to the last entry.
DEFAULT_BACKOFF_S: tuple[int, ...] = (30, 300, 1800)
PAUSE_KEY = "worker_paused"

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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _iso_in(seconds: float) -> str:
    """ISO timestamp ``seconds`` from now (negative = in the past). Same format/zone as ``_now``
    so it compares lexicographically against stored timestamps."""
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


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
    def __init__(
        self,
        db: sqlite3.Connection,
        *,
        max_attempts: int = 3,
        backoff_schedule: tuple[int, ...] | list[int] = DEFAULT_BACKOFF_S,
    ) -> None:
        self.db = db
        self.max_attempts = max_attempts
        self.backoff_schedule = tuple(backoff_schedule) or DEFAULT_BACKOFF_S
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
        if "not_before" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN not_before TEXT")
        if "revives" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN revives INTEGER NOT NULL DEFAULT 0")

    def _backoff_for(self, attempts: int) -> int:
        """Seconds to wait before the next attempt, given the number of attempts so far."""
        idx = max(0, min(attempts - 1, len(self.backoff_schedule) - 1))
        return self.backoff_schedule[idx]

    # --- pause / meta ---------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.db.execute("DELETE FROM meta WHERE key=?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            self.db.commit()

    def is_paused(self) -> bool:
        return self.get_meta(PAUSE_KEY) == "1"

    def pause(self) -> None:
        self.set_meta(PAUSE_KEY, "1")

    def resume(self) -> None:
        self.set_meta(PAUSE_KEY, None)

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
        """Atomically move the oldest eligible queued job to processing and return it.

        Returns None while the worker is paused, and skips jobs whose ``not_before`` (retry
        backoff / low-space deferral) is still in the future.
        """
        with self._lock:
            if self.get_meta(PAUSE_KEY) == "1":
                return None
            row = self.db.execute(
                "SELECT * FROM jobs WHERE status=? AND (not_before IS NULL OR not_before<=?) "
                "ORDER BY created_at ASC LIMIT 1",
                (QUEUED, _now()),
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
        """Record a failure; requeue (with backoff) if attempts remain, else dead-letter.
        Returns the new status."""
        with self._lock:
            row = self.db.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
            attempts = row["attempts"] if row else self.max_attempts
            requeue = attempts < self.max_attempts
            status = QUEUED if requeue else DEAD
            not_before = _iso_in(self._backoff_for(attempts)) if requeue else None
            self.db.execute(
                "UPDATE jobs SET status=?, current_step=NULL, error=?, progress=NULL, "
                "progress_note=NULL, not_before=?, updated_at=? WHERE id=?",
                (status, error, not_before, _now(), job_id),
            )
            self.db.commit()
            return status

    def defer(self, job_id: str, backoff_s: float, *, note: str | None = None) -> None:
        """Requeue a job to run no earlier than ``backoff_s`` from now, WITHOUT counting an
        attempt. Used for conditions that will clear on their own (e.g. low disk space), so a
        job waits for the world to change instead of burning its retry budget and dead-lettering.
        """
        with self._lock:
            self.db.execute(
                "UPDATE jobs SET status=?, current_step=NULL, error=?, progress=NULL, "
                "progress_note=NULL, not_before=?, updated_at=? WHERE id=?",
                (QUEUED, note, _iso_in(backoff_s), _now(), job_id),
            )
            self.db.commit()

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
                "UPDATE jobs SET status=?, attempts=0, error=NULL, not_before=NULL, updated_at=? "
                "WHERE id=? AND status IN (?,?,?)",
                (QUEUED, _now(), job_id, DEAD, FAILED, CANCELED),
            )
            self.db.commit()
            return cur.rowcount > 0

    def retry_all_dead(self) -> int:
        """Manually requeue every dead/failed job at once (dashboard 'Retry all dead')."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE jobs SET status=?, attempts=0, error=NULL, not_before=NULL, updated_at=? "
                "WHERE status IN (?,?)",
                (QUEUED, _now(), DEAD, FAILED),
            )
            self.db.commit()
            return cur.rowcount

    def revive_dead(self, *, cooldown_s: float, max_revives: int) -> int:
        """Auto-requeue dead jobs that have sat idle past ``cooldown_s`` and haven't already been
        auto-revived ``max_revives`` times. Bounded so a genuinely-broken job can't loop forever.
        """
        if max_revives <= 0:
            return 0
        with self._lock:
            cur = self.db.execute(
                "UPDATE jobs SET status=?, attempts=0, error=NULL, not_before=NULL, "
                "revives=revives+1, updated_at=? "
                "WHERE status=? AND revives<? AND updated_at<=?",
                (QUEUED, _now(), DEAD, max_revives, _iso_in(-cooldown_s)),
            )
            self.db.commit()
            return cur.rowcount

    def prune_old(self, days: int) -> int:
        """Delete finished (done/canceled) jobs older than ``days``. Returns rows removed."""
        if days <= 0:
            return 0
        with self._lock:
            cur = self.db.execute(
                "DELETE FROM jobs WHERE status IN (?,?) AND updated_at<=?",
                (DONE, CANCELED, _iso_in(-days * 86400)),
            )
            self.db.commit()
            return cur.rowcount

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
                "UPDATE jobs SET status=?, not_before=NULL, updated_at=? WHERE status=?",
                (QUEUED, _now(), PROCESSING),
            )
            self.db.commit()
            return cur.rowcount
