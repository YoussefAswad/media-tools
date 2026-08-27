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
from mediacore.dashdot import DashdotClient, SystemSnapshot
from mediacore.docker_status import ContainerStatus, DockerStatusClient
from mediacore.jellyfin import JellyfinClient
from mediacore.models import ArrInstance
from mediacore.notify import Notifier
from mediacore.qbit import QbitClient
from mediacore.scrutiny import DriveStatus, ScrutinyClient

from orchestrator.alerts import AlertEngine, Channel, InAppChannel, NtfyChannel
from orchestrator.config import (
    MonitoringConfig,
    OrchestratorConfig,
    load_orchestrator_config,
)
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
    docker: DockerStatusClient | None = None
    system: DashdotClient | None = None
    #: latest container snapshot produced by the worker's docker collection (the web routes read
    #: this instead of hitting the socket per request). ``docker_ok``: None = not collected yet,
    #: True = last poll succeeded, False = socket unreachable. Reference swaps are atomic, so no
    #: lock is needed for the single writer (worker) / many readers (web) pattern.
    docker_snapshot: list[ContainerStatus] | None = None
    docker_ok: bool | None = None
    #: latest host system snapshot from dashdot (same single-writer/many-readers pattern).
    system_snapshot: SystemSnapshot | None = None
    system_ok: bool | None = None
    scrutiny: ScrutinyClient | None = None
    #: latest SMART drive snapshot from Scrutiny (same single-writer/many-readers pattern).
    smart_snapshot: list[DriveStatus] | None = None
    smart_ok: bool | None = None
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
            self.docker = _docker_client(new_cfg.monitoring)
            if self.docker is None:  # disabled → drop any stale snapshot
                self.docker_snapshot = None
                self.docker_ok = None
            self.system = _system_client(new_cfg.monitoring)
            if self.system is None:
                self.system_snapshot = None
                self.system_ok = None
            self.scrutiny = _scrutiny_client(new_cfg.monitoring)
            if self.scrutiny is None:
                self.smart_snapshot = None
                self.smart_ok = None


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
    docker = _docker_client(config.monitoring)
    system = _system_client(config.monitoring)
    scrutiny = _scrutiny_client(config.monitoring)
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
        docker=docker,
        system=system,
        scrutiny=scrutiny,
    )


def _docker_client(mon: MonitoringConfig) -> DockerStatusClient | None:
    """A Docker socket client when container monitoring is enabled, else None."""
    if not mon.docker.enabled:
        return None
    return DockerStatusClient(str(mon.docker.socket))


def _system_client(mon: MonitoringConfig) -> DashdotClient | None:
    """A dashdot client when system monitoring is enabled, else None."""
    if not mon.system.enabled:
        return None
    return DashdotClient(mon.system.url)


def _scrutiny_client(mon: MonitoringConfig) -> ScrutinyClient | None:
    """A Scrutiny client when SMART monitoring is enabled, else None."""
    if not mon.smart.enabled:
        return None
    return ScrutinyClient(mon.smart.url)
