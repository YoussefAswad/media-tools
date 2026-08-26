"""Minimal qBittorrent WebUI API client (read-only, best-effort).

Used to snapshot a torrent's consumed bandwidth (downloaded + uploaded) at import time, before
the user deletes the torrent. Like :class:`mediacore.notify.Notifier`, every call is best-effort:
failures are logged and return ``None`` rather than raising, so they never break the pipeline.

The target instance has ``WebUI\\HostHeaderValidation=true``, so requests send a matching
``Referer`` header. The session cookie is reused and re-authenticated on a 403.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from mediacore.models import QbitConfig

log = logging.getLogger(__name__)


class QbitClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()
        # HostHeaderValidation requires a Referer that matches the WebUI address.
        self.session.headers.update({"Referer": self.base_url})
        self._authed = False

    @classmethod
    def from_config(cls, cfg: QbitConfig) -> QbitClient:
        return cls(cfg.base_url, cfg.username, cfg.password)

    def login(self) -> bool:
        try:
            r = self.session.post(
                f"{self.base_url}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=self.timeout,
            )
            # Success signalling varies by build: some return 200 "Ok.", others 204 No Content.
            # A wrong password returns 200 "Fails."; a banned IP returns 403.
            self._authed = r.status_code in (200, 204) and r.text.strip().lower() != "fails."
            if not self._authed:
                log.warning("qbit login failed: %s %s", r.status_code, r.text[:80])
            return self._authed
        except requests.RequestException as exc:
            log.warning("qbit login error: %s", exc)
            return False

    def _get(self, path: str, params: dict[str, Any]) -> Any | None:
        for attempt in (1, 2):
            if not self._authed and not self.login():
                return None
            try:
                r = self.session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                log.warning("qbit GET %s error: %s", path, exc)
                return None
            if r.status_code == 403 and attempt == 1:
                self._authed = False  # session expired; re-auth once
                continue
            if not r.ok:
                log.warning("qbit GET %s -> %s", path, r.status_code)
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    def torrent_by_hash(self, infohash: str) -> dict[str, Any] | None:
        """Return the transfer stats for one torrent, or None if unavailable.

        The arr ``downloadId`` is an uppercase hash; qBittorrent keys torrents by the lowercase
        hash, so we normalize before querying and matching.
        """
        if not infohash:
            return None
        h = infohash.lower()
        data = self._get("/api/v2/torrents/info", {"hashes": h})
        if not isinstance(data, list) or not data:
            return None
        t = next((x for x in data if str(x.get("hash", "")).lower() == h), data[0])
        return {
            "name": t.get("name", ""),
            "downloaded": int(t.get("downloaded") or 0),
            "uploaded": int(t.get("uploaded") or 0),
            "total_size": int(t.get("total_size") or 0),
            "category": t.get("category") or "",
            "completion_on": int(t.get("completion_on") or 0),
            "added_on": int(t.get("added_on") or 0),
            "state": t.get("state") or "",
            "hash": h,
        }
