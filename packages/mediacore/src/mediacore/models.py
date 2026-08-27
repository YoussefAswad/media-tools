"""Shared configuration models.

The unified config is composed of small, tool-agnostic sections. Secrets are kept out of
the file itself: any string may reference an environment variable as ``${VAR}`` or
``${VAR:-default}`` and is expanded at load time (see :mod:`mediacore.config`). This keeps
``config.yaml`` safe to edit in the web UI — the file holds env *references*, not secrets.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

# Canonical policy defaults, mirroring the original arr-quality-check.sh knobs.
DEFAULT_AUDIO_ALLOW: set[str] = {"ac3", "eac3", "truehd", "mlp", "dts"}
DEFAULT_SUBTITLE_KEEP_LANGS: set[str] = {"eng", "en", "ara", "ar"}


class ArrInstance(BaseModel):
    """A Radarr or Sonarr instance plus optional local↔remote path translation.

    ``media_root`` is the library path prefix as the *arr* reports it (in the webhook
    payload / its own DB); ``local_media_root`` is where this container mounts the same
    library. When both are set, paths are translated in each direction (see
    :mod:`mediacore.paths`). Leave both unset when the mounts are identical.
    """

    base_url: str
    api_key: str
    media_root: Path | None = None
    local_media_root: Path | None = None

    @model_validator(mode="after")
    def _both_or_neither(self) -> ArrInstance:
        if (self.media_root is None) != (self.local_media_root is None):
            raise ValueError("media_root and local_media_root must both be set or both omitted")
        return self


class ArrConfig(BaseModel):
    radarr: ArrInstance | None = None
    sonarr: ArrInstance | None = None


class NtfyConfig(BaseModel):
    url: str = "http://ntfy:80"
    topic: str = "media-quality"
    #: When set, per-action status notifications go here instead of ``topic``.
    status_topic: str | None = None
    user: str | None = None
    password: str | None = None
    #: ``job`` = one rollup per file; ``action`` = one per step; ``off`` = only failures.
    status_granularity: Literal["job", "action", "off"] = "job"

    def status_topic_resolved(self) -> str:
        return self.status_topic or self.topic


class PolicyConfig(BaseModel):
    """Quality-check + pruning policy (shared by quality-check and the prune step)."""

    audio_allow: set[str] = set(DEFAULT_AUDIO_ALLOW)
    audio_min_channels: int = 2
    audio_max_tracks: int = 2
    subtitle_keep_langs: set[str] = set(DEFAULT_SUBTITLE_KEEP_LANGS)
    #: Whether an untagged/``und`` subtitle counts as foreign (default: keep it).
    subtitle_flag_untagged: bool = False
    #: Guard: never drop the subtitle currently flagged default, even if foreign.
    keep_default_subtitle: bool = False
    #: Remux mp4-family containers (.mp4/.m4v/.mov) to MKV even when nothing else needs pruning,
    #: so the library stays pure-MKV. The usual subtitle/audio policies still apply on top.
    remux_to_mkv: bool = False


class QbitConfig(BaseModel):
    """qBittorrent WebUI connection, used to snapshot per-download bandwidth at import."""

    base_url: str = "http://qbittorrent:45666"
    username: str = ""
    password: str = ""


class JellyfinConfig(BaseModel):
    """Jellyfin connection, used to trigger a library refresh after an import."""

    base_url: str = "http://jellyfin:8096"
    api_key: str = ""


class AuditConfig(BaseModel):
    #: Root for the SQLite db, JSONL audit files, and seasonal-shift undo files.
    state_dir: Path = Path("/state")


class WebConfig(BaseModel):
    #: Optional app-level basic auth for the UI routes (in addition to Caddy).
    basic_auth_user: str | None = None
    basic_auth_password: str | None = None


class CoreConfig(BaseModel):
    """The shared config sections. Tools extend this (e.g. the orchestrator adds shows)."""

    arr: ArrConfig = ArrConfig()
    ntfy: NtfyConfig = NtfyConfig()
    policy: PolicyConfig = PolicyConfig()
    audit: AuditConfig = AuditConfig()
    web: WebConfig = WebConfig()
    #: qBittorrent connection for the download bandwidth ledger (None = ledger capture disabled).
    qbit: QbitConfig | None = None
    #: Jellyfin connection for post-import library refresh (None = refresh disabled).
    jellyfin: JellyfinConfig | None = None
    #: Shared secret ARR must send (custom header) for webhook requests to be accepted.
    webhook_secret: str | None = None
