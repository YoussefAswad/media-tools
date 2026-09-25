"""One choke point for every notification the orchestrator sends.

Eight places used to call ``services.notifier.send`` directly — the quality step, three places in
the dispatcher, three in the worker, and the alert engine's ntfy channel. Each decided its own
priority and topic, and only two of them had any config knob, so the loudest emitter of all
(quality findings: 60% of a measured 12-hour window) could not be switched off at all.

Routing them through :class:`Notifications` puts *whether to send*, *at what priority*, *to which
topic* and *have we already said this* in one place, driven by
:class:`orchestrator.config.NotificationsConfig`. The emitters keep owning what a message
**says**; this owns what happens to it. Adding a notification = one ``send()`` call with a new
event name plus one :class:`~orchestrator.config.NotifyRule` field on the config section.

Like the notifier it wraps, every call here is best-effort: a delivery failure is logged and
returns ``False``, never raised at the caller.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
import threading
from typing import TYPE_CHECKING

from mediacore.db import add_columns
from mediacore.notify import NtfyMessage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from orchestrator.config import NotifyRule
    from orchestrator.services import Services

log = logging.getLogger(__name__)

#: Every event name that can be sent. A name not in here still works (it falls back to a default
#: rule) but gets no config field, so keep the two in step.
EVENTS = (
    "quality-findings",
    "job-step",
    "job-success",
    "job-failed",
    "job-deferred",
    "library-scan",
    "quota",
    "quota-person",
    "monitoring",
    "wan-reconcile",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_sent (
    key TEXT PRIMARY KEY,
    ts  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notify_sent_ts ON notify_sent(ts);
"""

#: columns added after the table's first release — see :func:`mediacore.db.add_columns`.
_NOTIFY_SENT_ADDED: dict[str, str] = {}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _iso(value: _dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


class Notifications:
    """Decides, addresses, de-dupes and sends. Reads config fresh on every call so a Config-tab
    save takes effect without a restart, exactly like :meth:`Services.alert_channels`."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.db: sqlite3.Connection = services.db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            add_columns(self.db, "notify_sent", _NOTIFY_SENT_ADDED)
            self.db.commit()

    # --- policy ---------------------------------------------------------------------
    def rule(self, event: str) -> NotifyRule:
        return self.services.config.notifications.rule(event)

    def enabled(self, event: str) -> bool:
        cfg = self.services.config.notifications
        return bool(cfg.enabled and self.rule(event).enabled)

    # --- dedupe ---------------------------------------------------------------------
    def _dedupe_key(self, event: str, rule: NotifyRule, job_id: str | None, path: str | None) -> str | None:
        """The suppression key for this send, or None when this event doesn't de-dupe.

        ``once_per`` names *which* identity collapses repeats. A retry re-runs the pipeline under
        the same job id, so ``job`` is what silences an attempt storm; ``path`` is broader and
        also covers the same file arriving via a different job.
        """
        subject = {"job": job_id, "path": path}.get(rule.once_per)
        return f"{event}:{rule.once_per}:{subject}" if subject else None

    def _already_sent(self, key: str, window_s: int) -> bool:
        cutoff = _iso(_now() - _dt.timedelta(seconds=window_s))
        row = self.db.execute(
            "SELECT ts FROM notify_sent WHERE key=? AND ts>=?", (key, cutoff)
        ).fetchone()
        return row is not None

    def _mark_sent(self, key: str) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO notify_sent (key, ts) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET ts=excluded.ts",
                (key, _iso(_now())),
            )
            self.db.commit()

    def prune_old(self, days: int) -> int:
        """Drop suppression records older than ``days``. 0 keeps them forever."""
        if days <= 0:
            return 0
        cutoff = _iso(_now() - _dt.timedelta(days=days))
        with self._lock:
            cur = self.db.execute("DELETE FROM notify_sent WHERE ts < ?", (cutoff,))
            self.db.commit()
            return cur.rowcount or 0

    # --- send -----------------------------------------------------------------------
    def send(
        self,
        event: str,
        *,
        title: str,
        body: str,
        tags: list[str],
        priority: int,
        topic: str | None = None,
        job_id: str | None = None,
        path: str | None = None,
    ) -> bool:
        """Send one notification, subject to this event's rule. Returns whether it went out.

        ``priority`` is the emitter's own choice; the rule overrides it when it sets one. Same
        for ``topic``. ``job_id``/``path`` are the identities ``once_per`` can collapse on — pass
        whichever the caller knows, the rule picks.
        """
        if not self.enabled(event):
            return False
        rule = self.rule(event)

        key = self._dedupe_key(event, rule, job_id, path)
        if key and self._already_sent(key, rule.once_window_s):
            log.debug("notification %s suppressed (already sent for %s)", event, key)
            return False

        sent = self.services.notifier.send(
            NtfyMessage(
                title=title,
                body=body,
                tags=tags,
                priority=rule.priority if rule.priority is not None else priority,
                topic=rule.topic or topic,
            )
        )
        # Only a delivered message suppresses the next one: if ntfy was down, the retry that
        # follows should still get its chance to tell you.
        if sent and key:
            self._mark_sent(key)
        return sent
