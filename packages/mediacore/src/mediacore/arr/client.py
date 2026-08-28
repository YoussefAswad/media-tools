"""Radarr/Sonarr v3 REST clients.

Generalizes seasonal-shift's Sonarr-only client into a shared base plus Radarr/Sonarr
subclasses. The base owns the session, ``/api/v3/command`` dispatch, and command polling;
subclasses add rescan/refresh and (Sonarr) the manual-import flow used by seasonal-shift.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import requests

from mediacore.models import ArrInstance

log = logging.getLogger(__name__)


class ArrError(RuntimeError):
    pass


class ArrClient:
    app = "arr"

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key})

    # --- low-level ------------------------------------------------------------------
    def _get(self, endpoint: str, **kwargs: Any) -> Any:
        r = self.session.get(f"{self.base_url}{endpoint}", timeout=self.timeout, **kwargs)
        if not r.ok:
            log.error("%s GET %s -> %s %s", self.app, endpoint, r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint: str, **kwargs: Any) -> Any:
        r = self.session.post(f"{self.base_url}{endpoint}", timeout=self.timeout, **kwargs)
        if not r.ok:
            log.error("%s POST %s -> %s %s", self.app, endpoint, r.status_code, r.text[:500])
        r.raise_for_status()
        return r.json()

    # --- commands -------------------------------------------------------------------
    def command(self, name: str, **body: Any) -> dict[str, Any]:
        return self._post("/api/v3/command", json={"name": name, **body})

    def wait_for_command(
        self, command_id: int, *, timeout: float = 300.0, poll_interval: float = 2.0
    ) -> None:
        deadline = time.monotonic() + timeout
        while True:
            result = self._get(f"/api/v3/command/{command_id}")
            status = result.get("status", "")
            if status == "completed":
                return
            if status == "failed":
                raise ArrError(f"{self.app} command {command_id} failed: {result.get('message', '')}")
            if time.monotonic() > deadline:
                raise ArrError(f"{self.app} command {command_id} timed out (status={status})")
            time.sleep(poll_interval)

    def rescan(self, item_id: int) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def refresh(self, item_id: int) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    def rescan_and_wait(self, item_id: int, *, timeout: float = 300.0) -> None:
        cmd = self.rescan(item_id)
        cid = cmd.get("id")
        if cid is not None:
            self.wait_for_command(int(cid), timeout=timeout)


class RadarrClient(ArrClient):
    app = "radarr"

    def titles(self) -> list[dict[str, Any]]:
        """Every movie, for the path -> original-language index."""
        return self._get("/api/v3/movie") or []

    def rescan(self, item_id: int) -> dict[str, Any]:
        return self.command("RescanMovie", movieIds=[item_id])

    def refresh(self, item_id: int) -> dict[str, Any]:
        return self.command("RefreshMovie", movieIds=[item_id])


class SonarrClient(ArrClient):
    app = "sonarr"

    def titles(self) -> list[dict[str, Any]]:
        """Every series, for the path -> original-language index."""
        return self._get("/api/v3/series") or []

    def rescan(self, item_id: int) -> dict[str, Any]:
        return self.command("RescanSeries", seriesId=item_id)

    def refresh(self, item_id: int) -> dict[str, Any]:
        return self.command("RefreshSeries", seriesId=item_id)

    # --- manual import (ported from seasonal-shift) ---------------------------------
    def get_series(self, show_name: str) -> tuple[int, str]:
        for series in self._get("/api/v3/series"):
            if series["title"].lower() == show_name.lower():
                return series["id"], series["path"]
        raise ArrError(f"Series not found: {show_name}")

    def get_episode_id(self, series_id: int, season: int, episode: int) -> int:
        episodes = self._get(
            "/api/v3/episode", params={"seriesId": series_id, "seasonNumber": season}
        )
        for ep in episodes:
            if ep["episodeNumber"] == episode:
                return ep["id"]
        raise ArrError(f"Episode not found S{season:02}E{episode:02}")

    def get_episode_file(self, episode_id: int) -> dict[str, Any] | None:
        episode = self._get(f"/api/v3/episode/{episode_id}")
        if not episode.get("hasFile"):
            return None
        return self._get(f"/api/v3/episodefile/{episode['episodeFileId']}")

    def detect_file(
        self,
        series_id: int,
        file_path: str | Path,
        season: int,
        episode_id: int,
        *,
        quality: dict[str, Any] | None = None,
        release_group: str | None = None,
    ) -> dict[str, Any]:
        path = Path(file_path)
        candidates = self._get(
            "/api/v3/manualimport",
            params={"folder": str(path.parent), "seriesId": series_id},
        )
        if not candidates:
            raise ArrError("Sonarr detected no files")
        candidate = next((c for c in candidates if c["path"] == str(path)), None)
        if candidate is None:
            raise ArrError(f"File not detected by Sonarr: {path}")
        candidate["seriesId"] = series_id
        candidate["seasonNumber"] = season
        candidate["episodeIds"] = [episode_id]
        if quality is not None:
            candidate["quality"] = quality
        if release_group is not None:
            candidate["releaseGroup"] = release_group
        result = self._post("/api/v3/manualimport", json=[candidate])
        return result[0]

    def import_file(
        self, series_id: int, episode_id: int, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        candidate["seriesId"] = series_id
        candidate["episodeIds"] = [episode_id]
        return self._post(
            "/api/v3/command",
            json={"name": "ManualImport", "files": [candidate], "importMode": "auto"},
        )


def client_for(app: str, instance: ArrInstance) -> ArrClient:
    if app == "radarr":
        return RadarrClient(instance.base_url, instance.api_key)
    if app == "sonarr":
        return SonarrClient(instance.base_url, instance.api_key)
    raise ValueError(f"unknown arr app: {app!r}")
