"""Per-device WAN usage — the durable record of how much internet each device consumed.

The router reports *cumulative* counters that reset on reboot and drop out of its host table when a
device goes quiet, so nothing durable can be read straight out of it. This module turns a stream of
counter snapshots into a daily per-device ledger by differencing consecutive polls, and keeps the
cursor it needs to do that.

**The engine only ever counts the difference between two values it has itself observed.** A counter
it cannot difference — a MAC with no cursor, or one that restarted below the cursor — is recorded as
a baseline and counts *nothing*. Crediting the whole current counter instead is a fabrication:
nothing in that number says when those bytes crossed the link, and it re-counts the device's entire
history every time it reappears. See :func:`_delta`, which is where that used to happen.

Two tables:

* ``wan_usage`` — ``(day, mac)`` totals. The durable record. **Never pruned**: it is the history
  a quota question is answered from, unlike ``metrics``, whose 30-day retention is right for
  sparklines and fatal here.
* ``wan_cursor`` — the last counter value seen per MAC. Small, one row per device, and a real
  table rather than a JSON blob in ``meta`` because it grows with the device count and because a
  cursor must **outlive** the device's absence from the host table (see :data:`_CURSOR_TTL`).

Storage is keyed by MAC because that is the raw fact the router reports. *Identity* — one row per
device, however many MACs a phone rotates through — is resolved at read time by
:func:`fold_identities`, so a rename or a new alias re-groups history retroactively instead of
freezing whatever the device was called when the bytes were counted.

Days are bucketed in UTC, matching :mod:`orchestrator.ledger` (whose ``completed_at`` is UTC ISO),
so the two can be compared on the same axis.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import logging
import sqlite3
import threading
from collections.abc import Iterable, Mapping
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

#: How long a cursor is kept after the device was last seen in the router's host table.
#:
#: Retention is the whole mechanism. The router *keeps* a device's counter while the device is
#: missing from ``STAT_ENTRY`` — measured directly: the same ``rx=3618567324`` was offered once a
#: minute for 23 hours. Dropping the cursor on the first missed poll therefore threw away the one
#: fact that tells "the counter kept running" (subtract) from "the counter restarted" (baseline),
#: and every reappearance re-counted the device's whole history: a measured 1,249 GB in one day on
#: a link whose physical ceiling is ~326 GB/day.
#:
#: The horizon itself is hygiene, not correctness — with :func:`_delta` refusing to count anything
#: it did not difference, every outcome is either an observed difference or a baseline, and both
#: err downwards. It exists to bound the table against MAC-randomising phones (this household
#: produced 20 MACs in 12 days) and to bound how long a stale baseline can sit against a counter
#: that may have wrapped meanwhile.
_CURSOR_TTL = _dt.timedelta(days=7)


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


@dataclass(frozen=True)
class DeviceUsage:
    """One device's usage over a queried window.

    ``macs`` is plural because one device is not one MAC: modern phones (and the Xiaomi TV box on
    this network) randomise their hardware address per association, so a single handset reaches the
    router under several. :func:`fold_identities` is what collapses them; :meth:`WanUsage.by_device`
    returns one MAC per row and leaves that decision to the caller.
    """

    macs: tuple[str, ...]
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
    #: MACs recorded as a baseline because there was no previous value to difference against.
    baselined: int = 0
    #: Cursors dropped because their device has not been seen for :data:`_CURSOR_TTL`.
    expired: int = 0
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

        * **Only an observed difference is counted.** A device with no cursor — a cold start, a MAC
          never seen before, or one whose cursor has expired — is baselined and counts nothing.
        * **A counter that went backwards** is a reset (reboot, or statistics toggled), not a
          32-bit wrap: a wrap needs 4.29 GB, which at any plausible link speed takes far longer
          than one poll interval. It re-bases and counts nothing, for the same reason as above.
        * **A cursor outlives its device's absence** from the host table (:data:`_CURSOR_TTL`), so a
          device that flaps in and out is differenced rather than re-counted.
        * **A delta larger than the link could physically have carried** since that device was
          last seen is dropped and re-baselined. The router emits exactly this after a settings
          change — a measured 27 GB in a single minute. Note this guard is a backstop, not the
          thing that makes the engine safe: it is only as tight as ``link_max_bytes_s``, and a
          value set generously silently disarms it.
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
            counted_rx = counted_tx = resets = rejected = baselined = 0

            for dev in devices:
                prev = cursor.get(dev.mac)
                d_rx, d_tx, outcome = _delta(dev, prev)
                if outcome == "reset":
                    resets += 1
                    log.info(
                        "wan: counter for %s (%s) restarted (rx %d -> %d) — re-baselined, "
                        "counted nothing",
                        dev.host, dev.mac, prev[0] if prev else 0, dev.rx,
                    )
                elif outcome == "baseline":
                    baselined += 1
                    if not cold:
                        log.info(
                            "wan: first sight of %s (%s) — baselined at rx=%d tx=%d, "
                            "counted nothing",
                            dev.host, dev.mac, dev.rx, dev.tx,
                        )
                if d_rx or d_tx:
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
                # Keep the in-memory cursor in step with what was just written, so ``prev`` means
                # "the most recent value seen for this MAC" even within one poll. Without this a
                # duplicated row would be differenced against the same stale value twice and both
                # halves added; with it the second row differences against the first, and every
                # outcome is safe because _delta refuses to count an undifferenced counter.
                cursor[dev.mac] = (dev.rx, dev.tx, iso)

            # A cursor is retired by age, never because its device missed a poll — see _CURSOR_TTL.
            # Legacy rows carrying the old empty-string default sort below any ISO stamp and are
            # swept here, which is right: an unknown age should baseline rather than difference.
            cutoff = (stamp - _CURSOR_TTL).isoformat(timespec="seconds")
            expired = max(
                0, self.db.execute("DELETE FROM wan_cursor WHERE seen_at < ?", (cutoff,)).rowcount
            )
            self.db.commit()

        if cold:
            log.info("wan: cold start — baselined %d devices, counted nothing", len(devices))
        if expired:
            log.info("wan: expired %d cursor(s) unseen for %s", expired, _CURSOR_TTL)
        return PollResult(
            counted_rx=counted_rx,
            counted_tx=counted_tx,
            devices=len(devices),
            resets=resets,
            rejected=rejected,
            baselined=baselined,
            expired=expired,
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
        """Household totals over the window.

        Deliberately no device *count*: "how many devices" is a question about identity, not about
        MACs, and it is answered by :func:`fold_identities` so the tile and the table below it
        cannot disagree.
        """
        clause, params = self._range_where(start, end)
        sql = "SELECT COALESCE(SUM(rx),0) rx, COALESCE(SUM(tx),0) tx FROM wan_usage"
        if clause:
            sql += " WHERE " + clause
        r = self.db.execute(sql, params).fetchone()
        return {
            "downloaded": r["rx"],
            "uploaded": r["tx"],
            "total": r["rx"] + r["tx"],
        }

    def first_day(self) -> str | None:
        """The earliest day this store has data for, or None when it is empty.

        The ledger reaches back months while this record starts the day the router source was
        switched on, so anything comparing the two has to clamp to this or it compares a long
        history against a short one and reports nonsense.
        """
        r = self.db.execute("SELECT MIN(day) d FROM wan_usage").fetchone()
        return r["d"] if r and r["d"] else None

    def by_device(
        self, *, start: str | None = None, end: str | None = None
    ) -> list[DeviceUsage]:
        """Per-MAC totals over the window, heaviest first.

        ``host``/``kind`` come from the most recent day that MAC appears, so a renamed device shows
        its current name rather than whatever it was called on the first day of the range — except
        that a day on which the router reported *no* hostname (it stores the raw MAC then, which
        happens while it is re-creating a host-table entry) never wins over a day that has a real
        one. Otherwise one device would split into two identities, one named and one a bare MAC.
        """
        clause, params = self._range_where(start, end)
        sql = (
            "SELECT mac, rx, tx, host, kind FROM ("
            "  SELECT mac, SUM(rx) OVER (PARTITION BY mac) rx, SUM(tx) OVER (PARTITION BY mac) tx,"
            "         host, kind, ROW_NUMBER() OVER (PARTITION BY mac"
            "           ORDER BY (host IS NOT NULL AND host <> mac) DESC, day DESC) rn"
            "  FROM wan_usage"
        )
        if clause:
            sql += " WHERE " + clause
        sql += ") WHERE rn = 1 ORDER BY (rx + tx) DESC"
        return [
            DeviceUsage(
                macs=(r["mac"],), host=r["host"] or r["mac"], kind=r["kind"] or "",
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


# --- identity ---------------------------------------------------------------------------


def display_name(aliases: Mapping[str, str] | None, mac: str, reported: str) -> str:
    """The name a device is shown *and grouped* under.

    Configured alias, else the hostname the router reported, else the MAC. One definition, shared
    by the stored table and the live snapshot, so the two panels never disagree about what a device
    is called. The MAC lookup is case-insensitive because aliases are hand-written in YAML while
    ``DeviceStats.mac`` is normalised to upper case.
    """
    if aliases:
        alias = aliases.get(mac) or aliases.get(mac.upper()) or aliases.get(mac.lower())
        if alias:
            return alias
    return reported or mac


def fold_identities(
    rows: Iterable[DeviceUsage], aliases: Mapping[str, str] | None = None
) -> list[DeviceUsage]:
    """Collapse per-MAC rows into one row per device, heaviest first.

    Pure, and deliberately not a method on :class:`WanUsage`: the store stays free of config, and
    the grouping rule — which is a *presentation* decision about what counts as one device — stays
    unit-testable without a database.
    """
    folded: dict[str, DeviceUsage] = {}
    for row in rows:
        name = display_name(aliases, row.macs[0], row.host)
        prev = folded.get(name)
        folded[name] = (
            DeviceUsage(macs=row.macs, host=name, kind=row.kind, rx=row.rx, tx=row.tx)
            if prev is None
            else DeviceUsage(
                macs=prev.macs + row.macs,
                host=name,
                # Rows arrive heaviest first, so the first MAC folded in is the busiest one and its
                # link type is the one worth showing.
                kind=prev.kind or row.kind,
                rx=prev.rx + row.rx,
                tx=prev.tx + row.tx,
            )
        )
    return sorted(folded.values(), key=lambda d: d.total, reverse=True)


def _delta(dev: DeviceStats, prev: tuple[int, int, str] | None) -> tuple[int, int, str]:
    """(rx delta, tx delta, outcome) for one device, outcome ``delta``/``baseline``/``reset``.

    **Only a difference between two observed values is ever counted.** A counter this engine cannot
    difference is baselined and contributes nothing, because nothing in a bare counter says when
    those bytes crossed the link. Crediting it looks harmless on paper and is not: the router keeps
    a device's counter while the device is missing from its host table, so a flapping device had its
    entire accumulated history re-counted on every reappearance — 1,249 GB recorded in one day on a
    link that tops out near 326 GB/day.

    The cost is real and small: at most one poll interval of a device's traffic goes unrecorded when
    it is first seen or when the router restarts a counter. That is the right direction to err —
    an under-count is a gap, an over-count is a fiction that looks like data.
    """
    if prev is None:
        return 0, 0, "baseline"
    p_rx, p_tx, _ = prev
    if dev.rx < p_rx or dev.tx < p_tx:
        # A reset (reboot, or statistics toggled), never a 32-bit wrap: a wrap needs 4.29 GB, which
        # RouterSource asserts cannot happen inside one poll interval.
        return 0, 0, "reset"
    return dev.rx - p_rx, dev.tx - p_tx, "delta"


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
