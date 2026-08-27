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


class OrchestratorConfig(CoreConfig):
    server: ServerConfig = ServerConfig()
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
