"""Metric time-series store — a generic (ts, name, value) table on the shared SQLite connection.

Names are namespaced ``domain.field:instance`` strings, e.g. ``disk.used:/data`` or
``disk.pct:/state``. Samples are written by the worker's collection step and read back by the
monitoring UI for current values and growth trends. Lives on the same ``_SerializedConnection``
as jobs/actions/downloads (see :func:`mediacore.audit.open_db`), so all access is serialized.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
from dataclasses import dataclass, field

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT NOT NULL,
    name  TEXT NOT NULL,
    value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_name_ts ON metrics(name, ts);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _iso_ago(days: float) -> str:
    return (_dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)).isoformat(timespec="seconds")


@dataclass
class Sample:
    """One metric reading. ``ts`` defaults to now so collectors just pass name+value."""

    name: str
    value: float
    ts: str = field(default_factory=_now)


class Metrics:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    def record_many(self, samples: list[Sample]) -> None:
        if not samples:
            return
        # NB: one execute() per row (each serialized by _SerializedConnection). executemany is
        # NOT wrapped by that connection subclass, so it would bypass the shared-access lock.
        with self._lock:
            for s in samples:
                self.db.execute(
                    "INSERT INTO metrics (ts, name, value) VALUES (?,?,?)",
                    (s.ts, s.name, s.value),
                )
            self.db.commit()

    def latest(self, name: str) -> tuple[str, float] | None:
        """(ts, value) of the most recent sample for ``name``, or None."""
        r = self.db.execute(
            "SELECT ts, value FROM metrics WHERE name=? ORDER BY ts DESC, id DESC LIMIT 1",
            (name,),
        ).fetchone()
        return (r["ts"], r["value"]) if r else None

    def series(
        self, name: str, *, since_days: float, end: str | None = None
    ) -> list[tuple[str, float]]:
        """Raw (ts, value) samples for ``name`` within the last ``since_days``, ascending."""
        params: list = [name, _iso_ago(since_days)]
        sql = "SELECT ts, value FROM metrics WHERE name=? AND ts>=?"
        if end:
            sql += " AND ts<=?"
            params.append(end)
        sql += " ORDER BY ts ASC"
        return [(r["ts"], r["value"]) for r in self.db.execute(sql, params).fetchall()]

    def daily_max(self, name: str, *, since_days: float) -> list[tuple[str, float]]:
        """(YYYY-MM-DD, max value that day) for ``name`` — a compact sparkline series. MAX (not
        AVG) so a disk-usage trend reflects each day's peak fill."""
        day = "substr(ts, 1, 10)"
        rows = self.db.execute(
            f"SELECT {day} d, MAX(value) v FROM metrics WHERE name=? AND ts>=? "
            f"GROUP BY {day} ORDER BY d ASC",
            (name, _iso_ago(since_days)),
        ).fetchall()
        return [(r["d"], r["v"]) for r in rows]

    def prune_old(self, days: int) -> int:
        """Delete samples older than ``days``. Returns rows removed; non-positive disables."""
        if days <= 0:
            return 0
        with self._lock:
            cur = self.db.execute("DELETE FROM metrics WHERE ts<=?", (_iso_ago(days),))
            self.db.commit()
            return cur.rowcount
