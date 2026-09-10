"""Shared runtime context: config + db + audit + notifications + queue + arr/qbit clients + ledger.

One instance is built at startup and shared by the FastAPI app and the worker thread. Config is
mutable (the UI can save + hot-reload it), so callers should read ``services.config`` fresh each
time rather than caching sub-objects.
"""

from __future__ import annotations

import datetime as _dt
import logging
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
from orchestrator.ledger import Ledger, QuotaPeriods
from orchestrator.library import LibraryIndex
from orchestrator.metrics import Metrics
from orchestrator.monitor import (
    MISSING,
    MonitorSource,
    SourceSnapshot,
    WanDeps,
    build_sources,
)
from orchestrator.notifications import Notifications
from orchestrator.queue import JobQueue
from orchestrator.quota import Attribution, attribute
from orchestrator.titles import TitleIndex
from orchestrator.wan_usage import WanUsage, fold_identities
from orchestrator.wan_usage import subtract as wan_subtract

log = logging.getLogger(__name__)


@dataclass
class Services:
    config: OrchestratorConfig
    config_path: Path
    db: sqlite3.Connection
    audit: Audit
    notifier: Notifier
    queue: JobQueue
    ledger: Ledger
    quota_periods: QuotaPeriods
    metrics: Metrics
    alerts: AlertEngine
    #: per-device WAN usage, built by differencing the router's cumulative counters.
    wan: WanUsage
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

    def _wan_deps(self) -> WanDeps:
        """The WAN source's two non-config dependencies: where to write, and the pause flag."""
        return WanDeps(usage=self.wan, get_meta=self.queue.get_meta)

    def snapshot(self, name: str) -> SourceSnapshot:
        """The named source's last collection, or a never-collected placeholder."""
        return self.snapshots.get(name, MISSING)

    def source(self, name: str) -> MonitorSource | None:
        return next((s for s in self.sources if s.name == name), None)

    def source_enabled(self, name: str) -> bool:
        src = self.source(name)
        return bool(src and src.enabled)

    def attribution(
        self,
        start: str | None,
        end: str | None = None,
        *,
        subtract: dict[str, tuple[int, int]] | None = None,
    ) -> Attribution:
        """Who the household's bandwidth counted for over a window.

        One seam, so the Quota tab and the worker's alert cannot answer the same question two
        ways — the failure that made ``ntfy.status_granularity`` gate two emitters and not the
        other six. Bounds are ISO timestamps, matching :meth:`Ledger.list`; only the date part
        reaches the WAN store, which is why ``subtract`` exists.

        ``subtract`` removes a per-MAC amount from the window's WAN totals. It is how a quota
        period's start day is divided at the renewal instant rather than surrendered whole: the
        WAN window still spans the full day, and the half belonging to the other period is taken
        off. Applied before folding, because the snapshot is keyed by MAC.
        """
        aliases = self.config.monitoring.router.device_aliases
        raw = self.wan.by_device(start=start, end=end)
        if subtract:
            raw = wan_subtract(raw, subtract)
        devices = fold_identities(raw, aliases)
        rows = self.ledger.list(start=start, end=end, limit=100_000)
        return attribute(
            devices, rows, self.config.quotas, wan_first_day=self.wan.first_day()
        )

    def capture_period_split(self, period_id: int) -> bool:
        """Snapshot a period's start day at the instant it begins, so the day divides exactly.

        Only meaningful *now*: :meth:`WanUsage.day_totals` reads a running accumulator, so it
        answers "what has happened today so far" and one poll later it answers something else.
        That is why this is called from the create path and never recomputed, and why a period
        whose start is edited afterwards has its snapshot discarded rather than reinterpreted.

        Returns False when there is nothing to capture — a period that starts at midnight needs
        no division, and one dated in the past cannot have its instant recovered.
        """
        period = self.quota_periods.get(period_id)
        if period is None or not period.mid_day:
            return False
        today = _dt.datetime.now(_dt.UTC).date().isoformat()
        if period.start_date != today:
            return False
        self.quota_periods.capture_split(period_id, self.wan.day_totals(period.start_date))
        log.info(
            "quota: captured the start-day split for period %d at %s UTC",
            period_id, period.start_time,
        )
        return True

    def period_subtraction(self, period, *, side: str) -> dict[str, tuple[int, int]]:
        """What to remove from a window's WAN totals so a split start day lands on one side.

        ``side='after'`` is the period that *begins* on the boundary day: it keeps the day minus
        the snapshot. ``side='before'`` is its predecessor, which ends there: it keeps only the
        snapshot, so what comes off is everything else that day.
        """
        if period is None or not period.split_captured:
            return {}
        snapshot = self.quota_periods.split(period.id)
        if side == "after":
            return snapshot
        totals = self.wan.day_totals(period.start_date)
        return {
            mac: (max(0, rx - snapshot.get(mac, (0, 0))[0]),
                  max(0, tx - snapshot.get(mac, (0, 0))[1]))
            for mac, (rx, tx) in totals.items()
        }

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
            self.sources = build_sources(new_cfg.monitoring, wan=self._wan_deps())
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
    quota_periods = QuotaPeriods(db)
    library = LibraryIndex(db)
    metrics = Metrics(db)
    alerts = AlertEngine(db)
    wan = WanUsage(db)
    qbit = QbitClient.from_config(config.qbit) if config.qbit else None
    jellyfin = JellyfinClient.from_config(config.jellyfin) if config.jellyfin else None
    services = Services(
        config=config,
        config_path=config_path,
        db=db,
        audit=audit,
        notifier=notifier,
        queue=queue,
        ledger=ledger,
        quota_periods=quota_periods,
        metrics=metrics,
        library=library,
        alerts=alerts,
        wan=wan,
        qbit=qbit,
        jellyfin=jellyfin,
    )
    services.sources = build_sources(config.monitoring, wan=services._wan_deps())
    _backfill_game_downloads(services)
    return services


#: ``meta`` key marking the one-shot historical attribution as done.
_GAMES_BACKFILL_KEY = "ledger_games_backfilled"


def _backfill_game_downloads(services: Services) -> None:
    """Attribute historical game downloads to the device that actually pulled them.

    Everything the ledger holds was downloaded either by this server or, for games, by the
    household's gaming PC — a fact about the past that no future row will need, so it is a
    one-shot migration rather than a rule. Every other row is left NULL, which resolves to
    ``quotas.server_device`` and so follows a rename instead of freezing the name of the day.

    Guarded by a ``meta`` key rather than by "are any rows NULL", so clearing a row's device by
    hand is not undone by the next restart.
    """
    device = services.config.quotas.games_device.strip()
    if not device or services.queue.get_meta(_GAMES_BACKFILL_KEY):
        return
    touched = services.ledger.backfill_devices(kind="game", device=device)
    services.queue.set_meta(_GAMES_BACKFILL_KEY, device)
    if touched:
        log.info("ledger: attributed %d historical game download(s) to %s", touched, device)
