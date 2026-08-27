"""Orchestrator config = the shared CoreConfig plus server settings and seasonal-shift shows.

Reuses mediacore's config sections and seasonal-shift's ``ShowConfig`` directly, so the one
``config.yaml`` drives every tool.
"""

from __future__ import annotations

from pathlib import Path

from mediacore.config import find_default_config, load_config
from mediacore.models import CoreConfig
from pydantic import BaseModel, Field
from seasonal_shift.models import ShowConfig


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000


class DiskWatch(BaseModel):
    """One watched filesystem, identified by a path visible *inside the container*.

    A breach is raised when EITHER the fill percentage OR the absolute free space crosses its
    threshold (whichever trips first). Absolute-free values are human strings ("300GB", "50GiB")
    parsed with :func:`orchestrator.util.parse_bytes`. Any threshold left ``None`` is ignored.
    """

    path: Path
    label: str
    warn_pct: int | None = 85
    crit_pct: int | None = 95
    warn_free: str | None = None
    crit_free: str | None = "50GB"


class AlertRule(BaseModel):
    """Debounce/hysteresis timing for a family of alerts (thresholds live on the metric itself,
    e.g. per-``DiskWatch``; for scalar metrics ``warn``/``crit`` are the thresholds)."""

    enabled: bool = True
    warn: float | None = None
    crit: float | None = None
    #: the raw condition must hold this long before a (non-ok) alert fires — kills transient spikes.
    sustain_s: int = 300
    #: return-to-normal must hold this long before a recovery notice fires — hysteresis vs flapping.
    clear_s: int = 120
    #: minimum re-notify interval while a breach persists (reminder throttle).
    cooldown_s: int = 3600


class DockerSource(BaseModel):
    """Container up/down monitoring via the Docker Engine API over the local UNIX socket. The
    socket must be bind-mounted into the container (read-only is enough) and the process must be
    in its group. Disabled by default — enabling it requires that mount."""

    enabled: bool = False
    socket: Path = Path("/var/run/docker.sock")
    #: how often container state is polled (seconds). Cheap (a local socket call).
    interval_s: int = 30
    #: container names to alert on when down/unhealthy. Empty = alert on every container seen.
    #: (The UI always displays *all* containers regardless of this list.)
    watch: list[str] = Field(default_factory=list)


class MonitoringConfig(BaseModel):
    """System-monitoring feature: watched disks + alert timing. Phase 1 covers disk usage; later
    phases add container/system/SMART sources onto the same collect→store→alert→UI pipeline."""

    enabled: bool = True
    #: how often disk usage is sampled (seconds). Cheap (one statvfs per path).
    disk_interval_s: int = 60
    #: delete metric samples older than this many days (0 = keep forever).
    retention_days: int = 30
    disks: list[DiskWatch] = Field(
        default_factory=lambda: [
            DiskWatch(path=Path("/data"), label="MASS"),
            DiskWatch(path=Path("/state"), label="VAULT"),
        ]
    )
    #: debounce/hysteresis for disk alerts (disks creep, so a long recovery hold + cooldown).
    disk_alert: AlertRule = AlertRule(sustain_s=120, clear_s=300, cooldown_s=21600)
    #: container up/down monitoring (needs the docker socket bind-mounted).
    docker: DockerSource = DockerSource()
    #: debounce/hysteresis for container alerts (short sustain survives a restart/redeploy).
    container_alert: AlertRule = AlertRule(sustain_s=60, clear_s=0, cooldown_s=1800)
    #: ntfy topic override for monitoring alerts (defaults to the status topic when unset).
    ntfy_topic: str | None = None


class OrchestratorConfig(CoreConfig):
    server: ServerConfig = ServerConfig()
    #: system monitoring (watched disks, thresholds, alert timing).
    monitoring: MonitoringConfig = MonitoringConfig()
    #: seasonal-shift show offsets (matched to a Sonarr series by ``name``).
    shows: list[ShowConfig] = Field(default_factory=list)

    # pipeline toggles
    enable_seasonal_shift: bool = True
    enable_quality_check: bool = True
    enable_prune: bool = True
    #: Capture download bandwidth from qBittorrent at import (requires the `qbit` config block).
    enable_ledger: bool = True

    #: max worker attempts before a job is dead-lettered.
    max_attempts: int = 3
    #: per-attempt requeue backoff in seconds (indexed by attempt, clamped to the last entry).
    retry_backoff_s: list[int] = Field(default_factory=lambda: [30, 300, 1800])
    #: path to the mkvprune executable (on PATH by default in the container image).
    mkvprune_executable: str = "mkvprune"

    # --- background maintenance (runs on the worker thread between jobs) ---
    #: how often the worker runs retention / dead-letter revive / quota checks.
    maintenance_interval_s: int = 300
    #: delete finished jobs + audit action rows older than this many days (0 = keep forever).
    #: JSONL audit files are always kept.
    retention_days: int = 90
    #: quota-usage percentages that trigger a one-shot ntfy alert per cutoff period.
    quota_alert_pcts: list[int] = Field(default_factory=lambda: [85, 100])
    #: auto-requeue a dead job once it has sat idle this long (0 disables via dead_revive_max).
    dead_revive_cooldown_s: int = 3600
    #: how many times a dead job may be auto-revived before it stays dead (0 disables).
    dead_revive_max: int = 1

    # --- prune disk-space guard ---
    #: require free space >= source_size * this before pruning (mkvprune's own floor is ~1.15).
    prune_min_free_multiplier: float = 1.3
    #: when space is short, requeue the job this many seconds later (without burning an attempt).
    prune_low_space_backoff_s: int = 1800


def load_orchestrator_config(path: Path | None = None) -> OrchestratorConfig:
    cfg_path = path or find_default_config()
    return load_config(OrchestratorConfig, cfg_path)
