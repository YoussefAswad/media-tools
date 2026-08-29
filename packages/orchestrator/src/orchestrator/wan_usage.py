"""Per-device WAN usage — the durable record of how much internet each device consumed.

The router reports *cumulative* counters that reset on reboot and vanish when a device leaves its
host table, so nothing durable can be read straight out of it. This module turns a stream of
counter snapshots into a daily per-device ledger by differencing consecutive polls, and keeps the
cursor it needs to do that.

Two tables:

* ``wan_usage`` — ``(day, mac)`` totals. The durable record. **Never pruned**: it is the history
  a quota question is answered from, unlike ``metrics``, whose 30-day retention is right for
  sparklines and fatal here.
* ``wan_cursor`` — the last counter value seen per MAC. Small, one row per device, and a real
  table rather than a JSON blob in ``meta`` because it grows with the device count and because
  "this device disappeared" wants to be a ``DELETE``.

Days are bucketed in UTC, matching :mod:`orchestrator.ledger` (whose ``completed_at`` is UTC ISO),
so the two can be compared on the same axis.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import sqlite3
import threading
from dataclasses import dataclass

from mediacore.db import add_columns
from mediacore.router import DeviceStats

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS wan_usage (
    day  TEXT NOT NULL,
    mac  TEXT NOT NULL,
    host TEXT,
    kind TEXT,
    rx   INTEGER NOT NULL DEFAULT 0,
    tx   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, mac)
);
CREATE INDEX IF NOT EXISTS idx_wan_usage_day ON wan_usage(day);

CREATE TABLE IF NOT EXISTS wan_cursor (
    mac     TEXT PRIMARY KEY,
    rx      INTEGER NOT NULL DEFAULT 0,
    tx      INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL DEFAULT ''
);
"""

#: columns added after each table's first release — see :func:`mediacore.db.add_columns`.
_WAN_USAGE_ADDED: dict[str, str] = {}
_WAN_CURSOR_ADDED: dict[str, str] = {}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


@dataclass(frozen=True)
class DeviceUsage:
    """One device's usage over a queried window."""

    mac: str
    host: str
    kind: str
    rx: int
    tx: int

    @property
    def total(self) -> int:
        return self.rx + self.tx


@dataclass(frozen=True)
class PollResult:
    """What one poll actually counted, for logging and the UI's 'last poll' line."""

    counted_rx: int = 0
    counted_tx: int = 0
    devices: int = 0
    #: MACs whose counter went backwards (router reboot / statistics toggled).
    resets: int = 0
    #: MACs whose delta exceeded what the link could physically carry, and was dropped.
    rejected: int = 0
    #: True when this poll only established a baseline and deliberately counted nothing.
    cold_start: bool = False

    @property
    def counted(self) -> int:
        return self.counted_rx + self.counted_tx


class WanUsage:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            add_columns(self.db, "wan_usage", _WAN_USAGE_ADDED)
            add_columns(self.db, "wan_cursor", _WAN_CURSOR_ADDED)
            self.db.commit()

    # --- the delta engine -------------------------------------------------------------

    def apply(
        self,
        devices: list[DeviceStats],
        *,
        link_max_bytes_s: int,
        now: _dt.datetime | None = None,
    ) -> PollResult:
        """Fold one poll of cumulative counters into the daily table.

        The rules, each of which exists because of a real observed case:

        * **Cold start** (no cursor at all) records a baseline and counts *nothing*. Counting the
          current values would credit every device's entire accumulated history to today.
        * **A new MAC**, once a cursor exists, counts its current value — the router's counter for
          a device starts when it first sees it, so that value really is new traffic.
        * **A counter that went backwards** is a reset (reboot, or statistics toggled), not a
          32-bit wrap: a wrap needs 4.29 GB, which at any plausible link speed takes far longer
          than one poll interval. The current value is counted and the cursor re-based.
        * **A delta larger than the link could physically have carried** since that device was
          last seen is dropped and re-baselined. The router emits exactly this after a settings
          change — a measured 27 GB in a single minute.
        """
        stamp = now or _now()
        iso = stamp.isoformat(timespec="seconds")
        day = iso[:10]
        with self._lock:
            cursor = {
                r["mac"]: (r["rx"], r["tx"], r["seen_at"])
                for r in self.db.execute("SELECT mac, rx, tx, seen_at FROM wan_cursor")
            }
            cold = not cursor
            counted_rx = counted_tx = resets = rejected = 0

            for dev in devices:
                prev = cursor.get(dev.mac)
                d_rx, d_tx, was_reset = _delta(dev, prev, cold)
                if was_reset:
                    resets += 1
                if not cold and (d_rx or d_tx):
                    allowance = _allowance(prev, stamp, link_max_bytes_s)
                    if d_rx > allowance or d_tx > allowance:
                        log.warning(
                            "wan: implausible delta for %s (%s): rx=%d tx=%d > %d — re-baselining",
                            dev.host, dev.mac, d_rx, d_tx, allowance,
                        )
                        d_rx = d_tx = 0
                        rejected += 1
                if d_rx or d_tx:
                    self.db.execute(
                        "INSERT INTO wan_usage (day, mac, host, kind, rx, tx) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(day, mac) DO UPDATE SET "
                        "rx = rx + excluded.rx, tx = tx + excluded.tx, "
                        "host = excluded.host, kind = excluded.kind",
                        (day, dev.mac, dev.host, dev.kind, d_rx, d_tx),
                    )
                    counted_rx += d_rx
                    counted_tx += d_tx
                self.db.execute(
                    "INSERT INTO wan_cursor (mac, rx, tx, seen_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(mac) DO UPDATE SET "
                    "rx=excluded.rx, tx=excluded.tx, seen_at=excluded.seen_at",
                    (dev.mac, dev.rx, dev.tx, iso),
                )

            # A device that left the host table keeps no cursor: if it returns, the router's
            # counter for it will have restarted, and a stale cursor would read as a huge delta.
            seen = {d.mac for d in devices}
            for mac in cursor.keys() - seen:
                self.db.execute("DELETE FROM wan_cursor WHERE mac=?", (mac,))
            self.db.commit()

        if cold:
            log.info("wan: cold start — baselined %d devices, counted nothing", len(devices))
        return PollResult(
            counted_rx=counted_rx,
            counted_tx=counted_tx,
            devices=len(devices),
            resets=resets,
            rejected=rejected,
            cold_start=cold,
        )

    # --- reads ------------------------------------------------------------------------

    def _range_where(self, start: str | None, end: str | None) -> tuple[str, list]:
        """``start``/``end`` are ISO timestamps (as the ledger uses); only their date part
        matters here, so the caller can pass the same bounds to both."""
        where: list[str] = []
        params: list = []
        if start:
            where.append("day >= ?")
            params.append(start[:10])
        if end:
            where.append("day <= ?")
            params.append(end[:10])
        return " AND ".join(where), params

    def totals(self, *, start: str | None = None, end: str | None = None) -> dict[str, int]:
        clause, params = self._range_where(start, end)
        sql = (
            "SELECT COALESCE(SUM(rx),0) rx, COALESCE(SUM(tx),0) tx, "
            "COUNT(DISTINCT mac) devices FROM wan_usage"
        )
        if clause:
            sql += " WHERE " + clause
        r = self.db.execute(sql, params).fetchone()
        return {
            "downloaded": r["rx"],
            "uploaded": r["tx"],
            "total": r["rx"] + r["tx"],
            "devices": r["devices"],
        }

    def by_device(
        self, *, start: str | None = None, end: str | None = None
    ) -> list[DeviceUsage]:
        """Per-device totals over the window, heaviest first.

        ``host``/``kind`` come from the most recent day that device appears, so a renamed device
        shows its current name rather than whatever it was called on the first day of the range.
        """
        clause, params = self._range_where(start, end)
        sql = (
            "SELECT mac, rx, tx, host, kind FROM ("
            "  SELECT mac, SUM(rx) OVER (PARTITION BY mac) rx, SUM(tx) OVER (PARTITION BY mac) tx,"
            "         host, kind, ROW_NUMBER() OVER (PARTITION BY mac ORDER BY day DESC) rn"
            "  FROM wan_usage"
        )
        if clause:
            sql += " WHERE " + clause
        sql += ") WHERE rn = 1 ORDER BY (rx + tx) DESC"
        return [
            DeviceUsage(
                mac=r["mac"], host=r["host"] or r["mac"], kind=r["kind"] or "",
                rx=r["rx"], tx=r["tx"],
            )
            for r in self.db.execute(sql, params).fetchall()
        ]

    def daily_series(
        self, *, start: str | None = None, end: str | None = None
    ) -> list[tuple[str, int, int]]:
        """(YYYY-MM-DD, rx, tx) per day, ascending — the same shape ``Ledger`` returns."""
        clause, params = self._range_where(start, end)
        sql = "SELECT day, COALESCE(SUM(rx),0) rx, COALESCE(SUM(tx),0) tx FROM wan_usage"
        if clause:
            sql += " WHERE " + clause
        sql += " GROUP BY day ORDER BY day ASC"
        return [(r["day"], r["rx"], r["tx"]) for r in self.db.execute(sql, params).fetchall()]


def _delta(
    dev: DeviceStats, prev: tuple[int, int, str] | None, cold: bool
) -> tuple[int, int, bool]:
    """(rx delta, tx delta, was_reset) for one device against its previous counters."""
    if cold or prev is None:
        # Cold start baselines silently; a new MAC mid-run counts what the router has for it.
        return (0, 0, False) if cold else (dev.rx, dev.tx, False)
    p_rx, p_tx, _ = prev
    if dev.rx < p_rx or dev.tx < p_tx:
        return dev.rx, dev.tx, True
    return dev.rx - p_rx, dev.tx - p_tx, False


def _allowance(
    prev: tuple[int, int, str] | None, now: _dt.datetime, link_max_bytes_s: int
) -> int:
    """The most bytes the link could have carried since this device was last seen.

    Scaled by the real gap rather than the nominal poll interval, so a poll after the orchestrator
    was down for a day is not rejected as implausible.
    """
    elapsed = 60.0
    if prev and prev[2]:
        with contextlib.suppress(ValueError):
            elapsed = max(1.0, (now - _dt.datetime.fromisoformat(prev[2])).total_seconds())
    # A little headroom: the router's own sampling boundaries do not line up with our polls.
    return int(elapsed * link_max_bytes_s * 1.5) + 1_000_000
