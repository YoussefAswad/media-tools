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
    #: path to the mkvprune executable (on PATH by default in the container image).
    mkvprune_executable: str = "mkvprune"


def load_orchestrator_config(path: Path | None = None) -> OrchestratorConfig:
    cfg_path = path or find_default_config()
    return load_config(OrchestratorConfig, cfg_path)
