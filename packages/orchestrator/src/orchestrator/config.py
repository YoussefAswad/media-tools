"""Orchestrator config = the shared CoreConfig plus server settings and seasonal-shift shows.

Reuses mediacore's config sections and seasonal-shift's ``ShowConfig`` directly, so the one
``config.yaml`` drives every tool.
"""

from __future__ import annotations

from pathlib import Path

from mediacore.config import find_default_config, load_config
from mediacore.models import CoreConfig
from mediacore.renumber.models import ShowConfig
from pydantic import BaseModel, Field, field_validator


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
    #: how often container state is polled (seconds). The state list is cheap; per-container CPU/mem
    #: (``stats``) costs ~1s each but is fetched in parallel, so keep this comfortably above that.
    interval_s: int = 30
    #: also collect per-container CPU % and memory (Docker stats API). Adds ~1-2s per poll.
    stats: bool = True
    #: container names to alert on when down/unhealthy. Empty = alert on every container seen.
    #: (The UI always displays *all* containers regardless of this list.)
    watch: list[str] = Field(default_factory=list)


class SystemSource(BaseModel):
    """Host CPU / RAM / temps / network via the dashdot container's REST API. Reads dashdot (which
    already has privileged host access) instead of granting this container /proc + /sys. Disabled
    by default; enabling it needs dashdot reachable at ``url`` on the compose network."""

    enabled: bool = False
    url: str = "http://dash:3001"
    #: how often system metrics are polled (seconds). Cheap (a few small HTTP GETs).
    interval_s: int = 15


class SmartSource(BaseModel):
    """Drive SMART health + temperature via the Scrutiny container's API. Scrutiny's own collector
    has the raw device access; this just reads its cached summary. Disabled by default."""

    enabled: bool = False
    url: str = "http://scrutiny:8080"
    #: how often Scrutiny's cached summary is read (seconds). Cheap — it does not spin the disks
    #: (Scrutiny's collector polls smartctl on its own schedule). SMART data changes slowly.
    interval_s: int = 900


class MonitoringConfig(BaseModel):
    """System-monitoring feature: watched disks + alert timing. Phase 1 covers disk usage; later
    phases add container/system/SMART sources onto the same collect→store→alert→UI pipeline."""

    enabled: bool = True
    #: how often disk usage is sampled (seconds). Cheap (one statvfs per path).
    disk_interval_s: int = 30
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
    #: host system metrics via dashdot (CPU / RAM / temps / network).
    system: SystemSource = SystemSource()
    #: sustained high CPU temperature (°C). Long sustain ignores transient encode/prune spikes.
    cpu_temp_alert: AlertRule = AlertRule(warn=75, crit=85, sustain_s=300, clear_s=120)
    #: sustained high memory usage (%).
    mem_alert: AlertRule = AlertRule(warn=90, crit=97, sustain_s=300, clear_s=120)
    #: drive SMART health + temperature via Scrutiny.
    smart: SmartSource = SmartSource()
    #: SMART health failure → crit immediately (no sustain); re-notify at most daily.
    smart_alert: AlertRule = AlertRule(sustain_s=0, clear_s=0, cooldown_s=86400)
    #: sustained high drive temperature (°C).
    smart_temp_alert: AlertRule = AlertRule(warn=50, crit=60, sustain_s=900, clear_s=900)
    #: ntfy topic override for monitoring alerts (defaults to the status topic when unset).
    ntfy_topic: str | None = None


class LibraryRoot(BaseModel):
    """One library tree to index, as a path visible *inside the container*.

    ``app`` decides which arr a re-run from this root is addressed to (it shapes the synthesized
    event), so a root that mixes movies and episodes needs to be listed twice, once per subtree.
    """

    path: Path
    app: str = "radarr"
    label: str | None = None

    @field_validator("app")
    @classmethod
    def _known_app(cls, v: str) -> str:
        if v not in ("radarr", "sonarr"):
            raise ValueError("app must be 'radarr' or 'sonarr'")
        return v


class LibraryConfig(BaseModel):
    """The Library tab's back-catalogue index: which trees to walk, what counts as a media file,
    and how much a single bulk enqueue is allowed to queue at once."""

    enabled: bool = True
    #: trees to walk. Empty = derive one per configured arr instance from its
    #: ``local_media_root`` (falling back to ``media_root`` when the mounts are identical).
    roots: list[LibraryRoot] = Field(default_factory=list)
    #: file extensions the pipeline can act on (mkvprune's accepted inputs).
    extensions: list[str] = Field(default_factory=lambda: [".mkv", ".mp4", ".m4v", ".mov"])
    #: ignore anything smaller than this — samples, featurettes, stray artwork.
    min_size: str = "100MB"
    #: hard cap on one bulk enqueue, so a mis-click can't queue the whole library.
    max_enqueue: int = 200
    #: rows per page in the Library table.
    page_size: int = 100

    @field_validator("min_size", mode="before")
    @classmethod
    def _size_as_text(cls, v: object) -> object:
        """Accept a bare number from YAML (``min_size: 0``) as well as "100MB" — otherwise the
        obvious way to switch the floor off fails config validation at startup."""
        return str(v) if isinstance(v, int | float) else v

    @property
    def min_size_bytes(self) -> int:
        from orchestrator.util import parse_bytes

        return parse_bytes(self.min_size)


class OrchestratorConfig(CoreConfig):
    server: ServerConfig = ServerConfig()
    #: system monitoring (watched disks, thresholds, alert timing).
    monitoring: MonitoringConfig = MonitoringConfig()
    #: the Library tab's index of files already on disk (backfill source).
    library: LibraryConfig = LibraryConfig()
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

    # --- prune staging + disk-space guard ---
    #: directory for mkvprune's temp mux and audio encodes (mkvprune's ``--scratch-dir``).
    #: None = alongside the source, as before. Pointing this at fast local storage keeps the
    #: mux off the media disk; it must be writable by the orchestrator's UID and, in Docker,
    #: bind-mounted into the container. Note a scratch dir on another filesystem does not
    #: reduce what the media disk needs — finalize copies the output back (see
    #: replace.estimate_placement_bytes) — so both volumes are checked before a prune starts.
    prune_scratch_dir: str | None = None
    #: require free space >= source_size * this before pruning (mkvprune's own floor is ~1.15).
    prune_min_free_multiplier: float = 1.3
    #: when space is short, requeue the job this many seconds later (without burning an attempt).
    prune_low_space_backoff_s: int = 1800


def load_orchestrator_config(path: Path | None = None) -> OrchestratorConfig:
    cfg_path = path or find_default_config()
    return load_config(OrchestratorConfig, cfg_path)
