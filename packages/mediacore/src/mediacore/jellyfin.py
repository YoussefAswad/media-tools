"""Minimal Jellyfin client (best-effort) — triggers a library refresh after imports.

Like :class:`mediacore.notify.Notifier` / :class:`mediacore.qbit.QbitClient`, every call is
best-effort: failures are logged and return ``False`` rather than raising, so a refresh never
breaks the pipeline.
"""

from __future__ import annotations

import logging

import requests

from mediacore.models import JellyfinConfig

log = logging.getLogger(__name__)


class JellyfinClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: JellyfinConfig) -> JellyfinClient:
        return cls(cfg.base_url, cfg.api_key)

    def refresh_library(self) -> bool:
        """Trigger an incremental scan of all Jellyfin libraries. Returns True on success."""
        if not self.api_key:
            return False
        try:
            r = requests.post(
                f"{self.base_url}/Library/Refresh",
                headers={"X-Emby-Token": self.api_key},
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("jellyfin library refresh failed (%s): %s", self.base_url, exc)
            return False
