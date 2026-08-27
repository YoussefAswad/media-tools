"""Threshold alerting: a small, persisted state machine over monitored metrics.

Generalizes the one-shot quota alert in ``worker._check_quota`` into a reusable engine:

* Each *evaluation* names a stable ``key`` (e.g. ``disk:/data``) and its raw ``severity``
  (``ok``/``warn``/``crit``) computed by the caller from that metric's thresholds.
* The engine debounces (``sustain_s``), applies hysteresis on recovery (``clear_s``),
  de-dupes (only notifies on a *change* of the confirmed severity), and throttles reminders
  (``cooldown_s``) — all persisted in ``alert_state`` so it survives restarts.
* Notifications go to a pluggable list of :class:`Channel` (ntfy + in-app today; add Slack/email
  later without touching the engine). An append-only ``alert_events`` log backs the in-app view.

State lives on the shared ``_SerializedConnection`` alongside jobs/metrics.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import ClassVar, Protocol

from mediacore.notify import PRIORITY_FAILURE, PRIORITY_STATUS, Notifier, NtfyMessage

from orchestrator.config import AlertRule

log = logging.getLogger(__name__)

OK = "ok"
WARN = "warn"
CRIT = "crit"
_RANK = {OK: 0, WARN: 1, CRIT: 2}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_state (
    key             TEXT PRIMARY KEY,
    label           TEXT,
    candidate       TEXT NOT NULL DEFAULT 'ok',
    candidate_since TEXT,
    active          TEXT NOT NULL DEFAULT 'ok',
    notified_ts     TEXT,
    breach_since    TEXT,
    detail          TEXT,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    key      TEXT NOT NULL,
    label    TEXT,
    severity TEXT NOT NULL,
    detail   TEXT,
    kind     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_events_ts ON alert_events(ts);
"""


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _iso(dt: _dt.datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str | None) -> _dt.datetime | None:
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts)
    except ValueError:
        return None


@dataclass
class Evaluation:
    """One metric's current standing, computed by a collector: what to alert on, how bad it is
    right now (``severity``), a human ``detail``, and the timing ``rule`` to debounce it with."""

    key: str
    label: str
    severity: str  # ok | warn | crit
    detail: str
    rule: AlertRule


@dataclass
class AlertEvent:
    """A notify-worthy transition, dispatched to every channel."""

    key: str
    label: str
    severity: str  # new active severity (ok = recovery)
    detail: str
    kind: str  # alert | escalate | deescalate | recovery | reminder


class Channel(Protocol):
    def send(self, event: AlertEvent) -> None: ...


class InAppChannel:
    """Persists each transition to ``alert_events`` for the in-app history view."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    def send(self, event: AlertEvent) -> None:
        self.db.execute(
            "INSERT INTO alert_events (ts, key, label, severity, detail, kind) VALUES (?,?,?,?,?,?)",
            (_iso(_now()), event.key, event.label, event.severity, event.detail, event.kind),
        )
        self.db.commit()


class NtfyChannel:
    """Publishes a transition to ntfy (best-effort via the shared Notifier)."""

    _TAGS: ClassVar[dict[str, list[str]]] = {
        OK: ["white_check_mark"],
        WARN: ["warning"],
        CRIT: ["rotating_light"],
    }

    def __init__(self, notifier: Notifier, topic: str | None = None) -> None:
        self.notifier = notifier
        self.topic = topic

    def send(self, event: AlertEvent) -> None:
        recovered = event.severity == OK
        title = (
            f"{event.label} — recovered"
            if recovered
            else f"{event.label} — {event.severity.upper()}"
        )
        priority = PRIORITY_STATUS if event.severity != CRIT else PRIORITY_FAILURE
        self.notifier.send(
            NtfyMessage(
                title=title,
                body=event.detail or title,
                tags=self._TAGS.get(event.severity, []),
                priority=priority,
                topic=self.topic,
            )
        )


@dataclass
class ActiveAlert:
    key: str
    label: str
    severity: str
    detail: str
    since: str | None


class AlertEngine:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    # --- evaluation -----------------------------------------------------------------
    def evaluate(self, evals: list[Evaluation], channels: list[Channel]) -> None:
        """Advance the state machine for each evaluation and dispatch any transitions.

        Skips rules with ``enabled=False`` (a disabled rule stops firing but keeps its stored
        state, so re-enabling doesn't replay stale breaches)."""
        for ev in evals:
            if not ev.rule.enabled:
                continue
            try:
                event = self._step(ev)
            except Exception as exc:  # never let one bad metric wedge the collection loop
                log.warning("alert eval failed for %s: %s", ev.key, exc)
                continue
            if event is not None:
                self._dispatch(event, channels)

    def _step(self, ev: Evaluation) -> AlertEvent | None:
        now = _now()
        row = self.db.execute(
            "SELECT * FROM alert_state WHERE key=?", (ev.key,)
        ).fetchone()
        active = row["active"] if row else OK
        candidate = row["candidate"] if row else OK
        cand_since = _parse(row["candidate_since"]) if row else None
        breach_since = row["breach_since"] if row else None
        notified = _parse(row["notified_ts"]) if row else None

        # 1. track how long the raw severity has held (resets when it changes).
        if ev.severity != candidate or cand_since is None:
            candidate = ev.severity
            cand_since = now
        held = (now - cand_since).total_seconds()

        # 2. confirm a change of severity once it has been stable long enough (sustain for a
        #    worsening/other non-ok state, clear for a return to ok — hysteresis).
        rule = ev.rule
        event: AlertEvent | None = None
        new_active = active
        if candidate != active:
            need = rule.clear_s if candidate == OK else rule.sustain_s
            if held >= need:
                new_active = candidate
                kind = _transition_kind(active, candidate)
                event = AlertEvent(ev.key, ev.label, candidate, ev.detail, kind)
                notified = now
        elif active != OK and notified is not None:
            # 3. still breached at the same level → periodic reminder, throttled by cooldown.
            if (now - notified).total_seconds() >= rule.cooldown_s:
                event = AlertEvent(ev.key, ev.label, active, ev.detail, "reminder")
                notified = now

        # 4. maintain breach_since (when the current non-ok streak began).
        if new_active == OK:
            breach_since = None
        elif active == OK and new_active != OK:
            breach_since = _iso(now)

        self._save(
            ev, candidate, cand_since, new_active, notified, breach_since, now
        )
        return event

    def _save(
        self,
        ev: Evaluation,
        candidate: str,
        cand_since: _dt.datetime,
        active: str,
        notified: _dt.datetime | None,
        breach_since: str | None,
        now: _dt.datetime,
    ) -> None:
        with self._lock:
            self.db.execute(
                """INSERT INTO alert_state
                   (key, label, candidate, candidate_since, active, notified_ts, breach_since,
                    detail, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     label=excluded.label, candidate=excluded.candidate,
                     candidate_since=excluded.candidate_since, active=excluded.active,
                     notified_ts=excluded.notified_ts, breach_since=excluded.breach_since,
                     detail=excluded.detail, updated_at=excluded.updated_at""",
                (
                    ev.key,
                    ev.label,
                    candidate,
                    _iso(cand_since),
                    active,
                    _iso(notified) if notified else None,
                    breach_since,
                    ev.detail,
                    _iso(now),
                ),
            )
            self.db.commit()

    def _dispatch(self, event: AlertEvent, channels: list[Channel]) -> None:
        for ch in channels:
            try:
                ch.send(event)
            except Exception as exc:  # a broken channel must not block the others
                log.warning("alert channel %s failed: %s", type(ch).__name__, exc)

    # --- reads (UI) -----------------------------------------------------------------
    def active(self) -> list[ActiveAlert]:
        """Rows currently in a non-ok active state, worst first."""
        rows = self.db.execute(
            "SELECT * FROM alert_state WHERE active!=? ORDER BY breach_since ASC", (OK,)
        ).fetchall()
        out = [
            ActiveAlert(r["key"], r["label"], r["active"], r["detail"], r["breach_since"])
            for r in rows
        ]
        out.sort(key=lambda a: _RANK.get(a.severity, 0), reverse=True)
        return out

    def counts(self) -> dict[str, int]:
        """{'warn': n, 'crit': n} over currently-active alerts."""
        rows = self.db.execute(
            "SELECT active, COUNT(*) c FROM alert_state WHERE active!=? GROUP BY active", (OK,)
        ).fetchall()
        return {r["active"]: r["c"] for r in rows}

    def recent_events(self, *, limit: int = 50) -> list[dict]:
        """Most recent alert transitions (alert / escalate / recovery / reminder …), newest first."""
        rows = self.db.execute(
            "SELECT ts, label, severity, detail, kind FROM alert_events ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_old(self, days: int) -> int:
        """Delete alert_events older than ``days`` (the state table is kept — it's current status).
        Returns rows removed; non-positive disables."""
        if days <= 0:
            return 0
        cutoff = _iso(_now() - _dt.timedelta(days=days))
        with self._lock:
            cur = self.db.execute("DELETE FROM alert_events WHERE ts<=?", (cutoff,))
            self.db.commit()
            return cur.rowcount


def _transition_kind(old: str, new: str) -> str:
    if new == OK:
        return "recovery"
    if old == OK:
        return "alert"
    return "escalate" if _RANK[new] > _RANK[old] else "deescalate"
