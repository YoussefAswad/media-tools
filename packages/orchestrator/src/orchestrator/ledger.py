"""Downloads bandwidth ledger — a durable record of consumed bandwidth per download.

Rows are either captured automatically from qBittorrent at import time (``source='qbittorrent'``,
keyed by infohash) or added manually (``source='manual'``). "Consumed bandwidth" is
``downloaded_bytes + uploaded_bytes`` (not file size). Lives on the shared SQLite connection
(``mediacore.db.open_db``) alongside jobs + actions.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
from dataclasses import dataclass

from mediacore.db import add_columns

QBITTORRENT = "qbittorrent"
MANUAL = "manual"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,
    name             TEXT NOT NULL,
    kind             TEXT,
    app              TEXT,
    infohash         TEXT,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    uploaded_bytes   INTEGER NOT NULL DEFAULT 0,
    size_bytes       INTEGER NOT NULL DEFAULT 0,
    completed_at     TEXT,
    recorded_at      TEXT NOT NULL,
    notes            TEXT,
    edited           INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_hash ON downloads(infohash)
    WHERE infohash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_downloads_completed ON downloads(completed_at);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _iso_from_epoch(epoch: int) -> str | None:
    if not epoch or epoch <= 0:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.UTC).isoformat(timespec="seconds")


@dataclass
class DownloadRow:
    id: int
    source: str
    name: str
    kind: str | None
    app: str | None
    infohash: str | None
    downloaded_bytes: int
    uploaded_bytes: int
    size_bytes: int
    completed_at: str | None
    recorded_at: str
    notes: str | None
    edited: int

    @property
    def total_bytes(self) -> int:
        return self.downloaded_bytes + self.uploaded_bytes


class Ledger:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    # --- writes ---------------------------------------------------------------------
    def record_auto(
        self,
        *,
        infohash: str,
        name: str,
        kind: str | None,
        app: str | None,
        downloaded_bytes: int,
        uploaded_bytes: int,
        size_bytes: int = 0,
        completion_on: int = 0,
    ) -> None:
        """Upsert a qBittorrent-sourced row by infohash. A hand-edited row is left untouched."""
        h = infohash.lower()
        with self._lock:
            existing = self.db.execute(
                "SELECT id, edited FROM downloads WHERE infohash=?", (h,)
            ).fetchone()
            if existing and existing["edited"]:
                return  # user edited this row; don't clobber
            completed = _iso_from_epoch(completion_on) or _now()
            if existing:
                self.db.execute(
                    "UPDATE downloads SET name=?, kind=?, app=?, downloaded_bytes=?, "
                    "uploaded_bytes=?, size_bytes=?, completed_at=? WHERE id=?",
                    (name, kind, app, downloaded_bytes, uploaded_bytes, size_bytes,
                     completed, existing["id"]),
                )
            else:
                self.db.execute(
                    "INSERT INTO downloads (source, name, kind, app, infohash, downloaded_bytes, "
                    "uploaded_bytes, size_bytes, completed_at, recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (QBITTORRENT, name, kind, app, h, downloaded_bytes, uploaded_bytes,
                     size_bytes, completed, _now()),
                )
            self.db.commit()

    def add_manual(
        self,
        *,
        name: str,
        kind: str | None,
        downloaded_bytes: int,
        uploaded_bytes: int,
        size_bytes: int = 0,
        completed_at: str | None = None,
        notes: str | None = None,
    ) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO downloads (source, name, kind, app, infohash, downloaded_bytes, "
                "uploaded_bytes, size_bytes, completed_at, recorded_at, notes, edited) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                (MANUAL, name, kind, None, None, downloaded_bytes, uploaded_bytes, size_bytes,
                 completed_at or _now(), _now(), notes),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def update(self, row_id: int, **fields) -> bool:
        """Update any row (auto or manual) and mark it edited so re-imports won't overwrite it."""
        allowed = {
            "name", "kind", "downloaded_bytes", "uploaded_bytes", "size_bytes",
            "completed_at", "notes",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        cols = ", ".join(f"{k}=?" for k in sets)
        with self._lock:
            cur = self.db.execute(
                f"UPDATE downloads SET {cols}, edited=1 WHERE id=?",
                (*sets.values(), row_id),
            )
            self.db.commit()
            return cur.rowcount > 0

    def delete(self, row_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM downloads WHERE id=?", (row_id,))
            self.db.commit()
            return cur.rowcount > 0

    # --- reads ----------------------------------------------------------------------
    def _range_where(self, start: str | None, end: str | None) -> tuple[str, list]:
        where: list[str] = []
        params: list = []
        # Use completed_at, falling back to recorded_at, for time filtering.
        col = "COALESCE(completed_at, recorded_at)"
        if start:
            where.append(f"{col} >= ?")
            params.append(start)
        if end:
            where.append(f"{col} <= ?")
            params.append(end)
        return " AND ".join(where), params

    def get(self, row_id: int) -> DownloadRow | None:
        r = self.db.execute("SELECT * FROM downloads WHERE id=?", (row_id,)).fetchone()
        return DownloadRow(**dict(r)) if r else None

    def list(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = 500,
    ) -> list[DownloadRow]:
        clause, params = self._range_where(start, end)
        where = [clause] if clause else []
        if kind:
            where.append("kind = ?")
            params.append(kind)
        if source:
            where.append("source = ?")
            params.append(source)
        if q:
            where.append("(name LIKE ? OR notes LIKE ? OR kind LIKE ?)")
            params.extend([f"%{q}%"] * 3)
        sql = "SELECT * FROM downloads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Order by the download DAY (newest first), then by when the row was added — so several
        # entries sharing a completion date show the most-recently-added on top. id breaks any
        # final tie (later insert = higher id).
        sql += (
            " ORDER BY substr(COALESCE(completed_at, recorded_at), 1, 10) DESC,"
            " recorded_at DESC, id DESC LIMIT ?"
        )
        rows = self.db.execute(sql, (*params, limit)).fetchall()
        return [DownloadRow(**dict(r)) for r in rows]

    def totals(self, *, start: str | None = None, end: str | None = None) -> dict[str, int]:
        clause, params = self._range_where(start, end)
        sql = (
            "SELECT COUNT(*) c, COALESCE(SUM(downloaded_bytes),0) d, "
            "COALESCE(SUM(uploaded_bytes),0) u FROM downloads"
        )
        if clause:
            sql += " WHERE " + clause
        r = self.db.execute(sql, params).fetchone()
        return {"count": r["c"], "downloaded": r["d"], "uploaded": r["u"], "total": r["d"] + r["u"]}

    def daily_series(
        self, *, start: str | None = None, end: str | None = None
    ) -> list[tuple[str, int, int]]:
        """(YYYY-MM-DD, downloaded, uploaded) per day in range, ascending."""
        clause, params = self._range_where(start, end)
        day = "substr(COALESCE(completed_at, recorded_at), 1, 10)"
        sql = (
            f"SELECT {day} d, COALESCE(SUM(downloaded_bytes),0) dn, "
            "COALESCE(SUM(uploaded_bytes),0) up FROM downloads"
        )
        if clause:
            sql += " WHERE " + clause
        sql += f" GROUP BY {day} ORDER BY d ASC"
        return [(r["d"], r["dn"], r["up"]) for r in self.db.execute(sql, params).fetchall()]


# --- cutoffs ----------------------------------------------------------------------------

_CUTOFFS_SCHEMA = """
CREATE TABLE IF NOT EXISTS cutoffs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    cutoff_date  TEXT NOT NULL,
    limit_bytes  INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cutoffs_date ON cutoffs(cutoff_date);
"""

#: columns added after the table's first release — see :func:`mediacore.db.add_columns`.
_CUTOFFS_ADDED = {"alerted_pct": "INTEGER NOT NULL DEFAULT 0"}


@dataclass
class Cutoff:
    id: int
    cutoff_date: str
    limit_bytes: int
    note: str | None
    created_at: str
    alerted_pct: int = 0


class Cutoffs:
    """Period markers for the downloads ledger. The most recent cutoff defines the current
    quota period; an optional ``limit_bytes`` drives the 'remaining' card. ``alerted_pct`` tracks
    the highest quota-usage threshold already notified this period (so each alert fires once)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_CUTOFFS_SCHEMA)
            add_columns(self.db, "cutoffs", _CUTOFFS_ADDED)
            self.db.commit()

    def set_alerted(self, cutoff_id: int, pct: int) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE cutoffs SET alerted_pct=? WHERE id=?", (pct, cutoff_id)
            )
            self.db.commit()

    def add(self, *, cutoff_date: str, limit_bytes: int = 0, note: str | None = None) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO cutoffs (cutoff_date, limit_bytes, note, created_at) VALUES (?,?,?,?)",
                (cutoff_date, limit_bytes, note, _now()),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def update(
        self, cutoff_id: int, *, cutoff_date: str, limit_bytes: int = 0, note: str | None = None
    ) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE cutoffs SET cutoff_date=?, limit_bytes=?, note=? WHERE id=?",
                (cutoff_date, limit_bytes, note, cutoff_id),
            )
            self.db.commit()
            return cur.rowcount > 0

    def delete(self, cutoff_id: int) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM cutoffs WHERE id=?", (cutoff_id,))
            self.db.commit()
            return cur.rowcount > 0

    def list(self) -> list[Cutoff]:
        rows = self.db.execute(
            "SELECT * FROM cutoffs ORDER BY cutoff_date DESC, id DESC"
        ).fetchall()
        return [Cutoff(**dict(r)) for r in rows]

    def latest(self) -> Cutoff | None:
        r = self.db.execute(
            "SELECT * FROM cutoffs ORDER BY cutoff_date DESC, id DESC LIMIT 1"
        ).fetchone()
        return Cutoff(**dict(r)) if r else None

    def latest_date(self) -> str | None:
        c = self.latest()
        return c.cutoff_date if c else None
