"""Orchestrator config: the shared CoreConfig plus this service's own sections.

Grouped by job, because the flat layout mixed three of them at the YAML root: connection
settings (``arr``, ``ntfy``, ``qbit``, ``jellyfin``), media policy (``policy``), and operational
tuning — which is now ``worker`` (retries, retention, maintenance) and ``pipeline`` (which steps
run, in what order, and how prune is staged). The fifteen legacy root keys are still accepted;
see :meth:`OrchestratorConfig._lift_legacy_root_keys`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from mediacore.config import find_default_config, load_config
from mediacore.models import CoreConfig
from mediacore.renumber.models import ShowConfig
from pydantic import BaseModel, Field, field_validator, model_validator


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


class RouterSource(BaseModel):
    """Per-device WAN usage read from the router's own statistics table.

    This is the only source that knows how much *internet* each device in the house used. The
    obvious alternatives were measured and rejected: the host NIC counter is ~99.96% LAN traffic
    on this network, and the router's UPnP byte counters are fabricated. Disabled by default —
    enabling it needs the router password and costs a permanent admin session (see below).

    **The router allows one admin session.** Polling evicts anyone logged into the router's own
    web UI and vice versa, which is what the Bandwidth tab's pause control is for.
    """

    enabled: bool = False
    url: str = "http://192.168.1.1"
    username: str = "admin"
    password: str = ""
    #: how often the per-device counters are polled (seconds). One small HTTP round trip.
    interval_s: int = 60
    #: the fastest the WAN link could conceivably run, used to reject impossible counter jumps
    #: (the router emits a garbage record right after a settings change — a measured 27 GB in one
    #: minute). Generous is fine; this is a sanity bound, not a rate limit.
    link_max_bytes_s: int = 25_000_000
    #: re-enable the router's statistics feature at startup if a firmware update or factory reset
    #: turned it off. Reads first and writes only on a difference — these settings live in the
    #: router's flash.
    manage_settings: bool = True
    #: MAC -> friendly name, for devices whose reported hostname is unhelpful or which rotate
    #: their MAC (modern phones randomise, so one handset can appear as several devices).
    device_aliases: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _counters_cannot_wrap_between_polls(self) -> RouterSource:
        """The delta engine reads a counter going backwards as a reset, never as a 32-bit wrap.

        That is only safe while a wrap (4 GiB) cannot happen inside one poll interval, so the
        assumption is asserted here rather than left as a comment for someone to violate later by
        raising ``interval_s``.
        """
        if self.enabled and self.interval_s * self.link_max_bytes_s >= 4 * 1024**3:
            raise ValueError(
                f"router.interval_s ({self.interval_s}s) is too long for "
                f"link_max_bytes_s ({self.link_max_bytes_s} B/s): a 32-bit counter could wrap "
                "between polls and be miscounted as a reset. Lower interval_s."
            )
        return self


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
    #: per-device WAN usage via the router's statistics table (the Bandwidth tab).
    router: RouterSource = RouterSource()
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



class WorkerConfig(BaseModel):
    """How the single worker retries, defers, and tidies up after itself."""

    #: max attempts before a job is dead-lettered.
    max_attempts: int = 3
    #: per-attempt requeue backoff in seconds (indexed by attempt, clamped to the last entry).
    retry_backoff_s: list[int] = Field(default_factory=lambda: [30, 300, 1800])
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


class PipelineConfig(BaseModel):
    """What the per-file pipeline does, in order.

    ``steps`` is the pipeline: a step runs because it is in this list, which is why there are no
    ``enable_*`` booleans any more. Order is load-bearing — ledger first so bandwidth survives a
    later failure, prune before seasonal-shift so the rename moves a finished file — so it is
    written out rather than inferred.
    """

    steps: list[str] = Field(
        default_factory=lambda: ["ledger", "quality-check", "prune", "seasonal-shift", "jellyfin"]
    )
    #: path to the mkvprune executable (on PATH by default in the container image).
    mkvprune_executable: str = "mkvprune"
    #: directory for mkvprune's temp mux and audio encodes (its ``--scratch-dir``). None =
    #: alongside the source. Note a scratch dir on another filesystem does not reduce what the
    #: media disk needs — finalize copies the output back — so both volumes are checked.
    scratch_dir: str | None = None
    #: require free space >= source_size * this before pruning (mkvprune's own floor is ~1.15).
    min_free_multiplier: float = 1.3
    #: when space is short, requeue the job this many seconds later (without burning an attempt).
    low_space_backoff_s: int = 1800

    @field_validator("steps")
    @classmethod
    def _known_steps(cls, v: list[str]) -> list[str]:
        from orchestrator.steps import STEP_TYPES

        unknown = [n for n in v if n not in STEP_TYPES]
        if unknown:
            known = ", ".join(sorted(STEP_TYPES))
            raise ValueError(f"unknown pipeline step(s) {unknown}; known steps are: {known}")
        return v


class NotifyRule(BaseModel):
    """Delivery policy for one kind of notification.

    Every notification the orchestrator sends goes through :class:`orchestrator.notifications.
    Notifications`, which looks the sender up here. ``priority``/``topic`` left ``None`` mean
    "whatever the emitter asked for" — the rule only overrides when you set one.
    """

    enabled: bool = True
    #: ntfy priority override (1..5). None = the emitter's own choice.
    priority: int | None = None
    #: ntfy topic override. None = the ntfy section's status topic.
    topic: str | None = None
    #: Collapse repeats to one message per ``job`` or per ``path``. ``off`` sends every time.
    #: A retry re-runs the whole pipeline, so without this a job that is attempted 7 times sends
    #: its finding 7 times — measured: 30 files produced 210 finding messages plus 60
    #: dead-letter messages, 49% of a day's notifications.
    once_per: Literal["off", "job", "path"] = "off"
    #: How long a ``once_per`` suppression lasts. Retries of one job span hours at most.
    once_window_s: int = 86400


class NotificationsConfig(BaseModel):
    """Which notifications get sent, at what priority, and where.

    Split out from ``ntfy`` on purpose: ``ntfy`` is a *connection* (url, topic, credentials),
    this is *policy*, the same way ``arr`` is a connection and ``policy`` is what to do with a
    file. Before this section the only knob was ``ntfy.status_granularity``, which gated the job
    rollup and nothing else — the loudest emitter by far (quality findings, 60% of a measured
    day) had no switch at all.
    """

    enabled: bool = True

    #: Policy findings from quality-check (one message listing what the file violates).
    quality_findings: NotifyRule = NotifyRule(priority=3, once_per="job")
    #: One message per pipeline *step*. A firehose; off unless you are debugging the pipeline.
    job_step: NotifyRule = NotifyRule(enabled=False)
    #: The per-file rollup once the pipeline finishes: what each step did.
    job_success: NotifyRule = NotifyRule(priority=2)
    #: A job that ran out of attempts and was dead-lettered.
    job_failed: NotifyRule = NotifyRule(once_per="job")
    #: A prune deferred because the disk was too full (it retries on its own).
    job_deferred: NotifyRule = NotifyRule(once_per="job")
    #: A library scan finished (one summary for the whole walk).
    library_scan: NotifyRule = NotifyRule(priority=2)
    #: A bandwidth-quota threshold was crossed.
    quota: NotifyRule = NotifyRule()
    #: Monitoring alert transitions (disk / container / temperature / SMART).
    monitoring: NotifyRule = NotifyRule()

    #: When ``prune`` is in the pipeline it *fixes* what quality-check flags, and the rollup
    #: already restates the finding — measured over a real day, 153 of 153 rollups repeated the
    #: finding text a median 8 s after it, at a lower priority than the finding itself. So the
    #: standalone finding is pure duplication there. It is still sent when ``prune`` is not
    #: configured, which is the case where it is the only thing that tells you.
    suppress_findings_when_pruning: bool = True

    def rule(self, event: str) -> NotifyRule:
        """The rule for an event name (``job-success`` -> the ``job_success`` field)."""
        return getattr(self, event.replace("-", "_"), NotifyRule())


#: legacy root key -> (section, new field name). Kept so an existing config.yaml keeps working;
#: the file is only rewritten when a human edits it in the Config tab.
_LEGACY_KEYS = {
    "max_attempts": ("worker", "max_attempts"),
    "retry_backoff_s": ("worker", "retry_backoff_s"),
    "maintenance_interval_s": ("worker", "maintenance_interval_s"),
    "retention_days": ("worker", "retention_days"),
    "quota_alert_pcts": ("worker", "quota_alert_pcts"),
    "dead_revive_cooldown_s": ("worker", "dead_revive_cooldown_s"),
    "dead_revive_max": ("worker", "dead_revive_max"),
    "mkvprune_executable": ("pipeline", "mkvprune_executable"),
    "prune_scratch_dir": ("pipeline", "scratch_dir"),
    "prune_min_free_multiplier": ("pipeline", "min_free_multiplier"),
    "prune_low_space_backoff_s": ("pipeline", "low_space_backoff_s"),
}

#: legacy ``enable_x: false`` root key -> the step it used to switch off.
_LEGACY_TOGGLES = {
    "enable_ledger": "ledger",
    "enable_quality_check": "quality-check",
    "enable_prune": "prune",
    "enable_seasonal_shift": "seasonal-shift",
}


class OrchestratorConfig(CoreConfig):
    server: ServerConfig = ServerConfig()
    #: system monitoring (watched disks, thresholds, alert timing).
    monitoring: MonitoringConfig = MonitoringConfig()
    #: the Library tab's index of files already on disk (backfill source).
    library: LibraryConfig = LibraryConfig()
    #: seasonal-shift show offsets (matched to a Sonarr series by ``name``).
    shows: list[ShowConfig] = Field(default_factory=list)

    #: how the worker retries, defers and tidies up.
    worker: WorkerConfig = WorkerConfig()
    #: what the per-file pipeline does, in order.
    pipeline: PipelineConfig = PipelineConfig()
    #: which notifications are sent, at what priority, and where.
    notifications: NotificationsConfig = NotificationsConfig()

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_root_keys(cls, data: Any) -> Any:
        """Accept the pre-sections layout: fifteen loose scalars at the YAML root.

        They sat next to ``arr:`` and ``ntfy:`` mixing three different jobs — connection
        settings, media policy, operational tuning. Lifting them here means an existing
        config.yaml keeps working untouched; nothing rewrites the file behind the user's back.
        An explicit new-style section always wins over a legacy key.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        for old, (section, field_name) in _LEGACY_KEYS.items():
            if old not in data:
                continue
            value = data.pop(old)
            bucket = data.setdefault(section, {})
            if isinstance(bucket, dict) and field_name not in bucket:
                bucket[field_name] = value
        # ntfy.status_granularity used to be the only notification knob: it gated the job
        # rollup and the per-step ping, and nothing else. Map it onto the two rules that
        # replaced it. An explicit `notifications:` section wins, same as everywhere else.
        gran = data.get("ntfy", {}).get("status_granularity") if isinstance(data.get("ntfy"), dict) else None
        if gran in ("action", "off") and "notifications" not in data:
            data["notifications"] = {
                "job_step": {"enabled": gran == "action"},
                "job_success": {"enabled": False},
            }
        # enable_x: false used to switch a step off; membership of pipeline.steps does now
        off = [step for key, step in _LEGACY_TOGGLES.items() if data.pop(key, True) is False]
        if off:
            pipeline = data.setdefault("pipeline", {})
            if isinstance(pipeline, dict) and "steps" not in pipeline:
                pipeline["steps"] = [
                    s for s in PipelineConfig().steps if s not in off
                ]
        return data


def load_orchestrator_config(path: Path | None = None) -> OrchestratorConfig:
    cfg_path = path or find_default_config()
    return load_config(OrchestratorConfig, cfg_path)
