"""Downloads bandwidth ledger — a durable record of consumed bandwidth per download.

Rows are either captured automatically from qBittorrent at import time (``source='qbittorrent'``,
keyed by infohash) or added manually (``source='manual'``). "Consumed bandwidth" is
``downloaded_bytes + uploaded_bytes`` (not file size). Lives on the shared SQLite connection
(``mediacore.db.open_db``) alongside jobs + actions.

**The ledger does not measure the household's bandwidth — the router does.** These rows say what
a *slice* of that traffic was and who it was for; :mod:`orchestrator.quota` is what joins the two.
That is why a row carries ``device`` and ``quota``: the first says whose WAN total this download
came out of, the second lets it count somewhere else (a film downloaded on the server for someone
else in the house belongs to the shared pot, not to whoever's server it is).
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

#: columns added after the table's first release — see :func:`mediacore.db.add_columns`.
#:
#: Both are **nullable on purpose**, and neither carries a default. NULL is not "unknown", it is
#: *derive it*: a NULL ``device`` resolves to ``quotas.server_device`` and a NULL ``quota`` to
#: whatever quota that device currently counts to. Writing the resolved values into the rows
#: instead would freeze 38 rows against the server's name and its owner's quota on the day of the
#: migration, so renaming the server or moving a person between quotas would leave history behind
#: — the same reason :func:`orchestrator.wan_usage.fold_identities` resolves identity at read
#: time rather than stamping it at write time.
_DOWNLOADS_ADDED: dict[str, str] = {
    #: the folded device identity that performed this download.
    "device": "TEXT",
    #: the quota this download counts against, when that differs from the device's own.
    "quota": "TEXT",
}


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
    #: which device pulled this down. None = the configured server device.
    device: str | None = None
    #: which quota it counts against. None = whatever that device counts to.
    quota: str | None = None

    @property
    def total_bytes(self) -> int:
        return self.downloaded_bytes + self.uploaded_bytes

    @property
    def day(self) -> str:
        """The YYYY-MM-DD this download is booked against — the same expression the SQL uses."""
        return (self.completed_at or self.recorded_at)[:10]


class Ledger:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            add_columns(self.db, "downloads", _DOWNLOADS_ADDED)
            self.db.commit()

    def backfill_devices(self, *, kind: str, device: str) -> int:
        """One-shot: attribute historical rows of one kind to a device. Returns rows touched.

        Only rows with no device recorded are touched, so it never overrides a hand-set value.
        The caller guards it with a ``meta`` key rather than relying on that, so deliberately
        clearing a row back to NULL isn't undone by the next restart.
        """
        with self._lock:
            cur = self.db.execute(
                "UPDATE downloads SET device=? WHERE device IS NULL AND kind=?", (device, kind)
            )
            self.db.commit()
            return cur.rowcount

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
        device: str | None = None,
        quota: str | None = None,
    ) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO downloads (source, name, kind, app, infohash, downloaded_bytes, "
                "uploaded_bytes, size_bytes, completed_at, recorded_at, notes, edited, "
                "device, quota) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
                (MANUAL, name, kind, None, None, downloaded_bytes, uploaded_bytes, size_bytes,
                 completed_at or _now(), _now(), notes, device, quota),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def update(self, row_id: int, **fields) -> bool:
        """Update any row (auto or manual) and mark it edited so re-imports won't overwrite it."""
        allowed = {
            "name", "kind", "downloaded_bytes", "uploaded_bytes", "size_bytes",
            "completed_at", "notes", "device", "quota",
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

    def set_attribution(
        self, row_id: int, *, device: str | None, quota: str | None
    ) -> bool:
        """Set who downloaded this and which quota it counts against.

        Deliberately **not** through :meth:`update`, because that marks the row ``edited`` and a
        hand-edited row is never refreshed from qBittorrent again. Attribution says whose bytes
        these were, not how many there were; freezing the byte count because someone corrected
        the bookkeeping would be the wrong trade in the wrong direction.
        """
        with self._lock:
            cur = self.db.execute(
                "UPDATE downloads SET device=?, quota=? WHERE id=?", (device, quota, row_id)
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
        device: str | None = None,
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
        if device:
            where.append("device = ?")
            params.append(device)
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


# --- quota periods ------------------------------------------------------------------------

_QUOTA_PERIODS_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_periods (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    start_date   TEXT NOT NULL,
    limit_bytes  INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    created_at   TEXT NOT NULL,
    alerted_pct  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quota_periods_date ON quota_periods(start_date);

CREATE TABLE IF NOT EXISTS quota_alerts (
    period_id   INTEGER NOT NULL,
    quota       TEXT NOT NULL,
    alerted_pct INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (period_id, quota)
);
"""

#: columns added after each table's first release — see :func:`mediacore.db.add_columns`.
#:
#: NOT NULL carries a DEFAULT, which is not optional: SQLite accepts a bare NOT NULL on an empty
#: table and rejects it on a populated one, so the mistake passes in dev and fails at startup in
#: production. '00:00' also means an existing period keeps behaving exactly as it did.
_QUOTA_PERIODS_ADDED: dict[str, str] = {"start_time": "TEXT NOT NULL DEFAULT '00:00'"}
_QUOTA_ALERTS_ADDED: dict[str, str] = {}


def _rename_cutoffs(db: sqlite3.Connection) -> bool:
    """Carry a pre-rename database over: ``cutoffs(cutoff_date)`` -> ``quota_periods(start_date)``.

    **This has to run before the schema script, not after.** ``CREATE TABLE IF NOT EXISTS
    quota_periods`` would otherwise make an empty table, the guard below would see the destination
    already present and skip, and every existing period would sit orphaned in ``cutoffs`` while
    the UI showed none — a data-loss shape that looks like a clean install.

    Idempotent: it is a no-op once the rename has happened, and on a fresh database.
    """
    names = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "cutoffs" not in names or "quota_periods" in names:
        return False
    db.execute("ALTER TABLE cutoffs RENAME TO quota_periods")
    db.execute("ALTER TABLE quota_periods RENAME COLUMN cutoff_date TO start_date")
    # RENAME TO carries the table's indexes across under their old names; drop the old one so the
    # schema script's CREATE INDEX doesn't leave two indexes on the same column.
    db.execute("DROP INDEX IF EXISTS idx_cutoffs_date")
    db.commit()
    return True


@dataclass
class QuotaPeriod:
    id: int
    start_date: str
    limit_bytes: int
    note: str | None
    created_at: str
    alerted_pct: int = 0
    #: HH:MM (UTC) the period begins. A quota renewed at midday leaves that day split between
    #: two periods, and the downloads either side of it belong to different ones.
    start_time: str = "00:00"

    @property
    def starts_at(self) -> str:
        """The period's exact start as a UTC ISO timestamp — what the ledger is filtered on."""
        return f"{self.start_date}T{self.start_time or '00:00'}:00+00:00"

    @property
    def mid_day(self) -> bool:
        """True when the period starts partway through a day."""
        return (self.start_time or "00:00") != "00:00"

    @property
    def effective_start(self) -> str:
        """Where this period's accounting actually begins.

        For a period starting at midnight this is :attr:`starts_at`. For one renewed partway
        through a day it is the **next** midnight, because the start day cannot be divided:
        ``wan_usage`` stores one row per device per day, and there is no per-device sub-day
        record anywhere in this database to split it with. (``metrics`` is timestamped but
        household-aggregate and a calibrated estimate, so apportioning it per device would invent
        numbers — see pitfall 23.)

        Faced with a day that belongs to two periods, the whole of it goes to the **previous**
        one. That under-counts the new period by whatever ran between the renewal and midnight,
        and that is the direction to err: crediting the new period with the whole day would hand
        it a day of traffic that mostly predates it. An under-count is a gap; an over-count is a
        fiction that looks like data.

        :attr:`start_time` is not decorative under this rule — it is what decides that the start
        day belongs to the previous period at all.
        """
        if not self.mid_day:
            return self.starts_at
        nxt = _dt.date.fromisoformat(self.start_date) + _dt.timedelta(days=1)
        return f"{nxt.isoformat()}T00:00:00+00:00"

    @property
    def boundary_day(self) -> str | None:
        """The day this period surrenders to the previous one, or None when it starts cleanly."""
        return self.start_date if self.mid_day else None


class QuotaPeriods:
    """Period markers for the household's bandwidth. The most recent one defines the current
    quota period; an optional ``limit_bytes`` is the plan's cap and drives the 'remaining' card.

    ``alerted_pct`` tracks the highest household usage threshold already notified this period, so
    each alert fires once; ``quota_alerts`` does the same per individual quota, keyed by
    (period, quota) rather than crammed into the period row — the set of quotas is config, and
    config changes between periods.
    """

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            _rename_cutoffs(self.db)
            self.db.executescript(_QUOTA_PERIODS_SCHEMA)
            add_columns(self.db, "quota_periods", _QUOTA_PERIODS_ADDED)
            add_columns(self.db, "quota_alerts", _QUOTA_ALERTS_ADDED)
            self.db.commit()

    def set_alerted(self, period_id: int, pct: int) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE quota_periods SET alerted_pct=? WHERE id=?", (pct, period_id)
            )
            self.db.commit()

    def alerted_quotas(self, period_id: int) -> dict[str, int]:
        """quota key -> the highest percentage already notified for it this period."""
        rows = self.db.execute(
            "SELECT quota, alerted_pct FROM quota_alerts WHERE period_id=?", (period_id,)
        ).fetchall()
        return {r["quota"]: r["alerted_pct"] for r in rows}

    def set_quota_alerted(self, period_id: int, quota: str, pct: int) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO quota_alerts (period_id, quota, alerted_pct) VALUES (?,?,?) "
                "ON CONFLICT(period_id, quota) DO UPDATE SET alerted_pct=excluded.alerted_pct",
                (period_id, quota, pct),
            )
            self.db.commit()

    def add(
        self,
        *,
        start_date: str,
        limit_bytes: int = 0,
        note: str | None = None,
        start_time: str = "00:00",
    ) -> int:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO quota_periods (start_date, start_time, limit_bytes, note, created_at)"
                " VALUES (?,?,?,?,?)",
                (start_date, start_time or "00:00", limit_bytes, note, _now()),
            )
            self.db.commit()
            return int(cur.lastrowid or 0)

    def update(
        self,
        period_id: int,
        *,
        start_date: str,
        limit_bytes: int = 0,
        note: str | None = None,
        start_time: str = "00:00",
    ) -> bool:
        with self._lock:
            cur = self.db.execute(
                "UPDATE quota_periods SET start_date=?, start_time=?, limit_bytes=?, note=? "
                "WHERE id=?",
                (start_date, start_time or "00:00", limit_bytes, note, period_id),
            )
            self.db.commit()
            return cur.rowcount > 0

    def delete(self, period_id: int) -> bool:
        with self._lock:
            self.db.execute("DELETE FROM quota_alerts WHERE period_id=?", (period_id,))
            cur = self.db.execute("DELETE FROM quota_periods WHERE id=?", (period_id,))
            self.db.commit()
            return cur.rowcount > 0

    def list(self) -> list[QuotaPeriod]:
        rows = self.db.execute(
            "SELECT * FROM quota_periods ORDER BY start_date DESC, id DESC"
        ).fetchall()
        return [QuotaPeriod(**dict(r)) for r in rows]

    def latest(self) -> QuotaPeriod | None:
        r = self.db.execute(
            "SELECT * FROM quota_periods ORDER BY start_date DESC, id DESC LIMIT 1"
        ).fetchone()
        return QuotaPeriod(**dict(r)) if r else None

    def latest_date(self) -> str | None:
        p = self.latest()
        return p.start_date if p else None

    def latest_start(self) -> str | None:
        """Where the current period's accounting begins — :attr:`QuotaPeriod.effective_start`,
        not the raw renewal moment, so the ledger and the router's day-granular counters sit on
        one axis instead of disagreeing about who owns the start day."""
        p = self.latest()
        return p.effective_start if p else None

    def previous(self) -> QuotaPeriod | None:
        """The period before the current one — what the tab's 'Previous' range covers."""
        rows = self.db.execute(
            "SELECT * FROM quota_periods ORDER BY start_date DESC, id DESC LIMIT 2"
        ).fetchall()
        return QuotaPeriod(**dict(rows[1])) if len(rows) > 1 else None
