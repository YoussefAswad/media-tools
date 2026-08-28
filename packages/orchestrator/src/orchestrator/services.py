"""Shared runtime context: config + db + audit + notifications + queue + arr/qbit clients + ledger.

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
from mediacore.audit import Audit
from mediacore.db import open_db
from mediacore.jellyfin import JellyfinClient
from mediacore.models import ArrInstance
from mediacore.notify import Notifier
from mediacore.qbit import QbitClient

from orchestrator.alerts import AlertEngine, Channel, InAppChannel, NtfyChannel
from orchestrator.config import OrchestratorConfig, load_orchestrator_config
from orchestrator.ledger import Cutoffs, Ledger
from orchestrator.library import LibraryIndex
from orchestrator.metrics import Metrics
from orchestrator.monitor import MISSING, MonitorSource, SourceSnapshot, build_sources
from orchestrator.notifications import Notifications
from orchestrator.queue import JobQueue
from orchestrator.titles import TitleIndex


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
    #: index of what the pipeline would do to files already on disk (the Library tab).
    library: LibraryIndex
    #: path -> title/original-language, for the code paths that have no webhook payload.
    titles: TitleIndex = field(init=False)
    #: the one choke point every notification goes through (policy + de-dupe + delivery).
    notify: Notifications = field(init=False)
    qbit: QbitClient | None = None
    jellyfin: JellyfinClient | None = None
    #: every monitored source, enabled or not, each knowing its own interval.
    sources: list[MonitorSource] = field(default_factory=list)
    #: source name -> its last collection. Written only by the worker, read by the web
    #: threadpool; entries are replaced rather than mutated, so no lock is needed.
    snapshots: dict[str, SourceSnapshot] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.titles = TitleIndex(self)
        self.notify = Notifications(self)

    def snapshot(self, name: str) -> SourceSnapshot:
        """The named source's last collection, or a never-collected placeholder."""
        return self.snapshots.get(name, MISSING)

    def source(self, name: str) -> MonitorSource | None:
        return next((s for s in self.sources if s.name == name), None)

    def source_enabled(self, name: str) -> bool:
        src = self.source(name)
        return bool(src and src.enabled)

    def arr_instance(self, app: str) -> ArrInstance | None:
        return getattr(self.config.arr, app, None)

    def alert_channels(self) -> list[Channel]:
        """The enabled alert delivery channels, rebuilt each call so ntfy always reflects the
        current (hot-reloadable) config/notifier. Add Slack/email/webhook here."""
        return [
            InAppChannel(self.db),
            NtfyChannel(self.notify, self.config.monitoring.ntfy_topic),
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
            self.queue.max_attempts = new_cfg.worker.max_attempts
            self.queue.backoff_schedule = tuple(new_cfg.worker.retry_backoff_s) or self.queue.backoff_schedule
            self.qbit = QbitClient.from_config(new_cfg.qbit) if new_cfg.qbit else None
            self.jellyfin = (
                JellyfinClient.from_config(new_cfg.jellyfin) if new_cfg.jellyfin else None
            )
            # Rebuild every source against the new config, and drop the snapshot of any source
            # that is now switched off — keeping it would render stale numbers as if they were
            # live, which is exactly what the ok flag exists to prevent.
            self.sources = build_sources(new_cfg.monitoring)
            for src in self.sources:
                if not src.enabled:
                    self.snapshots.pop(src.name, None)


def build_services(config_path: Path) -> Services:
    config = load_orchestrator_config(config_path)
    db = open_db(config.audit.state_dir)
    audit = Audit(db, config.audit.state_dir)
    notifier = Notifier(config.ntfy)
    queue = JobQueue(
        db, max_attempts=config.worker.max_attempts, backoff_schedule=tuple(config.worker.retry_backoff_s)
    )
    queue.requeue_stuck()
    ledger = Ledger(db)
    cutoffs = Cutoffs(db)
    library = LibraryIndex(db)
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
        library=library,
        alerts=alerts,
        qbit=qbit,
        jellyfin=jellyfin,
        sources=build_sources(config.monitoring),
    )
