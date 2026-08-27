"""Shared runtime context: config + db + audit + notifier + queue + arr/qbit clients + ledger.

One instance is built at startup and shared by the FastAPI app and the worker thread. Config is
mutable (the UI can save + hot-reload it), so callers should read ``services.config`` fresh each
time rather than caching sub-objects.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from mediacore.arr.client import ArrClient, client_for
from mediacore.audit import Audit, open_db
from mediacore.jellyfin import JellyfinClient
from mediacore.models import ArrInstance
from mediacore.notify import Notifier
from mediacore.qbit import QbitClient

from orchestrator.alerts import AlertEngine, Channel, InAppChannel, NtfyChannel
from orchestrator.config import OrchestratorConfig, load_orchestrator_config
from orchestrator.ledger import Cutoffs, Ledger
from orchestrator.metrics import Metrics
from orchestrator.queue import JobQueue


@dataclass
class Services:
    config: OrchestratorConfig
    config_path: Path
    db: sqlite3.Connection
    audit: Audit
    notifier: Notifier
    queue: JobQueue
    ledger: Ledger
    cutoffs: Cutoffs
    metrics: Metrics
    alerts: AlertEngine
    qbit: QbitClient | None = None
    jellyfin: JellyfinClient | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def arr_instance(self, app: str) -> ArrInstance | None:
        return getattr(self.config.arr, app, None)

    def alert_channels(self) -> list[Channel]:
        """The enabled alert delivery channels, rebuilt each call so ntfy always reflects the
        current (hot-reloadable) config/notifier. Add Slack/email/webhook here."""
        return [
            InAppChannel(self.db),
            NtfyChannel(self.notifier, self.config.monitoring.ntfy_topic),
        ]

    def arr_client(self, app: str) -> ArrClient | None:
        inst = self.arr_instance(app)
        return client_for(app, inst) if inst else None

    def reload_config(self) -> None:
        """Re-read the config file and swap in the new values (hot reload)."""
        with self._lock:
            new_cfg = load_orchestrator_config(self.config_path)
            self.config = new_cfg
            self.notifier = Notifier(new_cfg.ntfy)
            self.queue.max_attempts = new_cfg.max_attempts
            self.queue.backoff_schedule = tuple(new_cfg.retry_backoff_s) or self.queue.backoff_schedule
            self.qbit = QbitClient.from_config(new_cfg.qbit) if new_cfg.qbit else None
            self.jellyfin = (
                JellyfinClient.from_config(new_cfg.jellyfin) if new_cfg.jellyfin else None
            )


def build_services(config_path: Path) -> Services:
    config = load_orchestrator_config(config_path)
    db = open_db(config.audit.state_dir)
    audit = Audit(db, config.audit.state_dir)
    notifier = Notifier(config.ntfy)
    queue = JobQueue(
        db, max_attempts=config.max_attempts, backoff_schedule=tuple(config.retry_backoff_s)
    )
    queue.requeue_stuck()
    ledger = Ledger(db)
    cutoffs = Cutoffs(db)
    metrics = Metrics(db)
    alerts = AlertEngine(db)
    qbit = QbitClient.from_config(config.qbit) if config.qbit else None
    jellyfin = JellyfinClient.from_config(config.jellyfin) if config.jellyfin else None
    return Services(
        config=config,
        config_path=config_path,
        db=db,
        audit=audit,
        notifier=notifier,
        queue=queue,
        ledger=ledger,
        cutoffs=cutoffs,
        metrics=metrics,
        alerts=alerts,
        qbit=qbit,
        jellyfin=jellyfin,
    )
