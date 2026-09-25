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
* ``wan_counters`` — the counter values seen for each MAC on the poll it was last present. Small,
  and a real table rather than a JSON blob in ``meta`` because it grows with the device count and
  because a cursor must **outlive** the device's absence from the host table (see
  :data:`_CURSOR_TTL`). Usually one row per MAC, but **not always**: the router can report the same
  MAC twice in one poll — a frozen entry it has not cleaned up beside the live, restarted one — and
  a single cursor per MAC turned that pair into fabricated traffic (see :func:`_match`).

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

CREATE TABLE IF NOT EXISTS wan_counters (
    mac     TEXT NOT NULL,
    rx      INTEGER NOT NULL DEFAULT 0,
    tx      INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_wan_counters_mac ON wan_counters(mac);
"""

#: columns added after each table's first release — see :func:`mediacore.db.add_columns`.
_WAN_USAGE_ADDED: dict[str, str] = {}
_WAN_COUNTERS_ADDED: dict[str, str] = {}

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

#: One stored counter: (rx, tx, seen_at ISO stamp).
_Counter = tuple[int, int, str]


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
    #: MACs the router reported more than once, with different counters, in this poll.
    duplicates: int = 0
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
            add_columns(self.db, "wan_counters", _WAN_COUNTERS_ADDED)
            self._migrate_cursor()
            self.db.commit()

    def _migrate_cursor(self) -> None:
        """Carry the one-per-MAC ``wan_cursor`` table over into ``wan_counters``, once.

        Copied rather than dropped, because dropping it would re-baseline every device — and a
        device that is absent right now would lose the retained cursor that lets its return be
        differenced instead of baselined.
        """
        legacy = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wan_cursor'"
        ).fetchone()
        if not legacy:
            return
        self.db.execute(
            "INSERT INTO wan_counters (mac, rx, tx, seen_at) "
            "SELECT mac, rx, tx, seen_at FROM wan_cursor"
        )
        self.db.execute("DROP TABLE wan_cursor")
        log.info("wan: migrated wan_cursor into wan_counters")

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
        by_mac: dict[str, list[DeviceStats]] = {}
        for dev in devices:
            rows = by_mac.setdefault(dev.mac, [])
            # The same counter reported twice is one counter, not two: collapse exact repeats so
            # neither copy is mistaken for a second series that "restarted".
            if not any(r.rx == dev.rx and r.tx == dev.tx for r in rows):
                rows.append(dev)

        with self._lock:
            stored: dict[str, list[_Counter]] = {}
            for r in self.db.execute("SELECT mac, rx, tx, seen_at FROM wan_counters"):
                stored.setdefault(r["mac"], []).append((r["rx"], r["tx"], r["seen_at"]))
            cold = not stored
            counted_rx = counted_tx = resets = rejected = baselined = duplicates = 0

            for mac, rows in by_mac.items():
                prevs = stored.get(mac, [])
                if len(rows) > 1:
                    duplicates += 1
                if len(rows) != max(len(prevs), 1) and (len(rows) > 1 or len(prevs) > 1):
                    # Logged on the transition only: a duplicated entry can persist for days, and
                    # one line a minute would bury everything else in the log.
                    log.info(
                        "wan: %s (%s) now reports %d counter(s), was %d",
                        rows[0].host, mac, len(rows), len(prevs),
                    )
                for dev, prev in _match(rows, prevs):
                    # No counter to difference against: a restart if the MAC had any, else new.
                    d_rx, d_tx, outcome = (
                        _delta(dev, prev) if prev is not None or not prevs else (0, 0, "reset")
                    )
                    if outcome == "reset":
                        resets += 1
                        log.info(
                            "wan: counter for %s (%s) restarted (rx %s -> %d) — re-baselined, "
                            "counted nothing",
                            dev.host, mac, "/".join(str(p[0]) for p in prevs), dev.rx,
                        )
                    elif outcome == "baseline":
                        baselined += 1
                        if not cold:
                            log.info(
                                "wan: first sight of %s (%s) — baselined at rx=%d tx=%d, "
                                "counted nothing",
                                dev.host, mac, dev.rx, dev.tx,
                            )
                    if d_rx or d_tx:
                        allowance = _allowance(prev, stamp, link_max_bytes_s)
                        if d_rx > allowance or d_tx > allowance:
                            log.warning(
                                "wan: implausible delta for %s (%s): rx=%d tx=%d > %d — "
                                "re-baselining",
                                dev.host, mac, d_rx, d_tx, allowance,
                            )
                            d_rx = d_tx = 0
                            rejected += 1
                    if d_rx or d_tx:
                        self.db.execute(
                            "INSERT INTO wan_usage (day, mac, host, kind, rx, tx) "
                            "VALUES (?,?,?,?,?,?) "
                            "ON CONFLICT(day, mac) DO UPDATE SET "
                            "rx = rx + excluded.rx, tx = tx + excluded.tx, "
                            "host = excluded.host, kind = excluded.kind",
                            (day, mac, dev.host, dev.kind, d_rx, d_tx),
                        )
                        counted_rx += d_rx
                        counted_tx += d_tx

                # A MAC present this poll is described by exactly the counters it reported: every
                # one of them is now the value to difference against, and a counter that did not
                # reappear is gone. A MAC *absent* this poll is not touched — see _CURSOR_TTL.
                self.db.execute("DELETE FROM wan_counters WHERE mac=?", (mac,))
                self.db.executemany(
                    "INSERT INTO wan_counters (mac, rx, tx, seen_at) VALUES (?,?,?,?)",
                    [(mac, dev.rx, dev.tx, iso) for dev in rows],
                )

            # A cursor is retired by age, never because its device missed a poll — see _CURSOR_TTL.
            # Legacy rows carrying the old empty-string default sort below any ISO stamp and are
            # swept here, which is right: an unknown age should baseline rather than difference.
            cutoff = (stamp - _CURSOR_TTL).isoformat(timespec="seconds")
            expired = max(
                0,
                self.db.execute("DELETE FROM wan_counters WHERE seen_at < ?", (cutoff,)).rowcount,
            )
            self.db.commit()

        if cold:
            log.info("wan: cold start — baselined %d devices, counted nothing", len(devices))
        if expired:
            log.info("wan: expired %d cursor(s) unseen for %s", expired, _CURSOR_TTL)
        return PollResult(
            counted_rx=counted_rx,
            counted_tx=counted_tx,
            devices=len(by_mac),
            resets=resets,
            rejected=rejected,
            baselined=baselined,
            expired=expired,
            duplicates=duplicates,
            cold_start=cold,
        )

    # --- reads ------------------------------------------------------------------------

    def _range_where(self, start: str | None, end: str | None) -> tuple[str, list]:
        """``start``/``end`` are ISO timestamps (as the ledger uses), reduced to whole days here.

        ``end`` is an **exclusive instant**, and truncating it to a date silently made it an
        inclusive day. One quota period ends exactly where the next begins, so a period boundary
        at midnight put that whole day in *both* windows: with the current period starting
        ``2026-09-11T00:00`` the previous one asked for ``day <= '2026-09-11'`` and the two
        overlapped by a full day. Each window still reconciled internally, so nothing flagged it
        — the day was simply counted twice, once on each side.

        So the last day included is the one containing ``end - 1s``. A bare ``YYYY-MM-DD`` end is
        left alone: it names a day rather than an instant (``totals(start=day, end=day)`` asks
        for exactly that day), and shifting it would move the answer back by one.
        """
        where: list[str] = []
        params: list = []
        if start:
            where.append("day >= ?")
            params.append(start[:10])
        if end:
            where.append("day <= ?")
            params.append(_last_day(end))
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

    def day_totals(self, day: str) -> dict[str, tuple[int, int]]:
        """One day's accumulated (rx, tx) per MAC, as of right now.

        Read at the instant a quota period is created, this *is* the traffic that predates it:
        :meth:`apply` accumulates into the day's row, so before the next poll the row holds
        exactly what has happened since midnight. One poll later it does not, which is why the
        caller stores the answer rather than recomputing it.
        """
        rows = self.db.execute(
            "SELECT mac, rx, tx FROM wan_usage WHERE day=?", (day,)
        ).fetchall()
        return {r["mac"]: (r["rx"], r["tx"]) for r in rows}

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


# --- reconciliation ---------------------------------------------------------------------

#: How far a day's recorded total may stray from the router's own sampled rate before it is
#: flagged. Measured here: clean days read 0.99-1.04, while the days a duplicated router row
#: poisoned read 4.50-8.28. 15% sits well clear of both.
RECONCILE_TOLERANCE = 0.15
#: Below this neither side is worth a verdict — a quiet day's ratio is mostly sampling noise.
_RECONCILE_MIN_BYTES = 1_000_000_000
#: A day with fewer samples than this share of the polls it should have had is not compared: while
#: the router is paused or unreachable ``metrics`` has a hole, but the delta engine differences
#: across the gap and records the traffic anyway, so the ratio would read high for no fault.
_RECONCILE_MIN_COVERAGE = 0.9


@dataclass(frozen=True)
class Reconciliation:
    """One day's recorded total held against an independent measurement of the same link."""

    day: str
    recorded: int
    sampled: int

    @property
    def ratio(self) -> float:
        return self.recorded / self.sampled if self.sampled else float("inf")

    @property
    def suspect(self) -> bool:
        return abs(self.ratio - 1.0) > RECONCILE_TOLERANCE


def reconcile(
    day: str, recorded: int, sampled_sum: float, samples: int, interval_s: int
) -> Reconciliation | None:
    """Check a day of ``wan_usage`` against the sum of the ``wan.rx``/``wan.tx`` samples.

    The samples come from ``currBytes``, a *different* field of the same router rows, read as a
    per-second rate over the router's own 60 s window, so one sample times the poll interval is
    roughly one poll's traffic (the factor measured against trusted days was 60.36 for a 60 s
    poll). They never pass through the delta engine, which is what makes them an independent
    check on it: this comparison is how both over-counting bugs were found — by hand, and the
    second one 12 days late. None means "no verdict": too little coverage, or too little traffic.
    """
    if interval_s <= 0:
        return None
    expected = 86_400 / interval_s
    if samples < expected * _RECONCILE_MIN_COVERAGE:
        return None
    sampled = int(sampled_sum * interval_s)
    if max(recorded, sampled) < _RECONCILE_MIN_BYTES:
        return None
    return Reconciliation(day=day, recorded=recorded, sampled=sampled)


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


def _last_day(end: str) -> str:
    """The last whole day an exclusive ``end`` instant covers. A bare date is that date."""
    if len(end) <= 10 or "T" not in end:
        return end[:10]
    try:
        moment = _dt.datetime.fromisoformat(end)
    except ValueError:
        return end[:10]
    return (moment - _dt.timedelta(seconds=1)).date().isoformat()


def subtract(
    rows: Iterable[DeviceUsage], adjust: Mapping[str, tuple[int, int]]
) -> list[DeviceUsage]:
    """Remove a per-MAC amount from per-MAC usage rows, clamped at zero.

    Used to divide a quota period's start day between the period and its predecessor, from the
    snapshot taken at the renewal instant. Pure and keyed by MAC because that is what
    :meth:`WanUsage.by_device` returns before :func:`fold_identities` groups it — subtracting
    after folding would need the snapshot to be folded too, by the same alias table, at the same
    moment, and the two would drift apart the first time an alias changed.

    Clamped because a snapshot can legitimately exceed a later window's total: the day it belongs
    to may have been re-counted, or the row deleted. Zero is the floor, never a negative.
    """
    out = []
    for row in rows:
        d_rx, d_tx = adjust.get(row.macs[0], (0, 0))
        rx, tx = max(0, row.rx - d_rx), max(0, row.tx - d_tx)
        if rx or tx:
            out.append(DeviceUsage(macs=row.macs, host=row.host, kind=row.kind, rx=rx, tx=tx))
    return sorted(out, key=lambda d: d.total, reverse=True)


def _match(
    rows: list[DeviceStats], prevs: list[_Counter]
) -> list[tuple[DeviceStats, _Counter | None]]:
    """Pair this poll's rows for one MAC with that MAC's stored counters, one to one.

    **One MAC can be two counters.** The router sometimes reports a MAC twice in one poll — a
    frozen entry it has not cleaned up (hostname ``Unknown`` or the bare MAC) beside the live,
    restarted one — and keeps doing so for days. Measured on logy: a stale ``rx=199743377`` next
    to a live counter climbing from 12 MB, every minute for 36 hours. With one cursor per MAC the
    two rows took turns overwriting it, so each poll differenced the stale row against the live
    value and counted the gap: 427 GB in two days for a phone that used under 1 GB. The
    plausibility bound caught only the devices whose gap happened to exceed one poll's allowance.

    So each row is differenced against the stored counter it *continues*: a pair is eligible only
    when neither direction went backwards, and the smallest delta wins. The frozen row then always
    pairs with itself (delta 0) and the live row with its own previous value. A row left without a
    partner counts nothing — :meth:`WanUsage.apply` treats it as a restart or a first sighting.

    For the usual single row per MAC this is exactly one comparison, the same as before.
    """
    eligible = sorted(
        ((dev.rx - p[0]) + (dev.tx - p[1]), i, j)
        for i, dev in enumerate(rows)
        for j, p in enumerate(prevs)
        if p[0] <= dev.rx and p[1] <= dev.tx
    )
    paired: dict[int, _Counter] = {}
    used: set[int] = set()
    for _, i, j in eligible:
        if i in paired or j in used:
            continue
        paired[i] = prevs[j]
        used.add(j)
    return [(dev, paired.get(i)) for i, dev in enumerate(rows)]


def _delta(dev: DeviceStats, prev: _Counter | None) -> tuple[int, int, str]:
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
    prev: _Counter | None, now: _dt.datetime, link_max_bytes_s: int
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
