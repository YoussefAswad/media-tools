"""Minimal Jellyfin client (best-effort) — keeps Jellyfin's view of a file honest after the
pipeline rewrites it.

The important distinction, learned the hard way: ``POST /Library/Refresh`` ("Scan Media Library")
is a **file-presence walk**. It finds files that were added, moved or deleted — measured on a real
2,231-episode library it completes in ~9 seconds — and it does *not* re-probe a file whose path is
unchanged. mkvprune replaces files in place, so after a prune Jellyfin keeps serving the track list
(and even the file size) it recorded at import, until someone hits "Refresh metadata" by hand.

That manual action is ``POST /Items/{id}/Refresh``, which is what this client does per file. The
library scan is kept as the fallback for the case it is actually right for: a path Jellyfin has
never seen (a new import, or a file the pipeline renamed).

Like :class:`mediacore.notify.Notifier` / :class:`mediacore.qbit.QbitClient`, every call is
best-effort: failures are logged and return ``False`` rather than raising.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

from mediacore.models import JellyfinConfig

log = logging.getLogger(__name__)

#: item types worth indexing for a path lookup (the things the pipeline touches).
_ITEM_TYPES = "Movie,Episode"


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        path_map: dict[str, str] | None = None,
        path_suffix_parts: int = 3,
        index_ttl_s: float = 900.0,
        refetch_min_interval_s: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.path_map = path_map or {}
        self.path_suffix_parts = max(1, path_suffix_parts)
        self.index_ttl_s = index_ttl_s
        # A file Jellyfin genuinely doesn't have (every new import) misses the index and would
        # otherwise refetch it per job; during a burst that is one full library listing each.
        self.refetch_min_interval_s = refetch_min_interval_s
        self._last_forced = 0.0
        self._lock = threading.Lock()
        self._by_path: dict[str, str] = {}
        self._by_suffix: dict[str, list[str]] = {}
        self._indexed_at = 0.0
        self._admin_id: str | None = None

    @classmethod
    def from_config(cls, cfg: JellyfinConfig) -> JellyfinClient:
        return cls(
            cfg.base_url,
            cfg.api_key,
            path_map=dict(cfg.path_map),
            path_suffix_parts=cfg.path_suffix_parts,
        )

    # --- http ------------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self.api_key}

    def _get(self, path: str, **params):
        r = requests.get(
            f"{self.base_url}{path}", headers=self._headers(), params=params, timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    # --- identity --------------------------------------------------------------------
    def _admin_user_id(self) -> str | None:
        """An administrator's user id. A non-admin only sees the libraries shared with them —
        on a real instance that turned 2,231 visible episodes into 10 — so the path index has to
        be built as an admin or it silently covers a fraction of the library."""
        if self._admin_id:
            return self._admin_id
        users = self._get("/Users")
        admin = next((u for u in users if (u.get("Policy") or {}).get("IsAdministrator")), None)
        self._admin_id = (admin or (users[0] if users else {})).get("Id")
        return self._admin_id

    # --- path -> item ----------------------------------------------------------------
    def _suffix(self, path: str) -> str:
        return "/".join(path.rstrip("/").split("/")[-self.path_suffix_parts :]).lower()

    def _build_index(self) -> None:
        uid = self._admin_user_id()
        if not uid:
            return
        items = self._get(
            "/Items",
            userId=uid,
            recursive="true",
            includeItemTypes=_ITEM_TYPES,
            fields="Path",
            limit=100000,
            enableTotalRecordCount="false",
        )
        by_path: dict[str, str] = {}
        by_suffix: dict[str, list[str]] = {}
        for item in items.get("Items") or []:
            path, item_id = item.get("Path"), item.get("Id")
            if not path or not item_id:
                continue
            by_path[path] = item_id
            by_suffix.setdefault(self._suffix(path), []).append(item_id)
        self._by_path, self._by_suffix = by_path, by_suffix
        self._indexed_at = time.monotonic()

    def _ensure_index(self, force: bool = False) -> None:
        if force or not self._by_path or (time.monotonic() - self._indexed_at) > self.index_ttl_s:
            self._build_index()

    def _mapped(self, local_path: str) -> str:
        """Apply the configured local -> Jellyfin prefix rewrite (longest prefix wins)."""
        for src in sorted(self.path_map, key=len, reverse=True):
            if local_path.startswith(src):
                return self.path_map[src] + local_path[len(src) :]
        return local_path

    def find_item(self, local_path: str, *, refresh_index: bool = True) -> str | None:
        """The Jellyfin item id for a local media path, or None.

        Tried in order: the configured path mapping, the raw path, then the trailing path
        components — which covers the common case of Jellyfin mounting the same library under a
        different prefix without any configuration. The suffix match is only trusted when exactly
        one item matches; a title that exists in two libraries is ambiguous and is left alone.
        """
        if not self.api_key:
            return None
        try:
            self._ensure_index()
            with self._lock:
                hit = self._by_path.get(self._mapped(local_path)) or self._by_path.get(local_path)
                if hit:
                    return hit
                candidates = self._by_suffix.get(self._suffix(local_path)) or []
            if len(candidates) == 1:
                return candidates[0]
            if candidates:
                log.info(
                    "jellyfin: %s matches %d items by path suffix; leaving it to a library scan",
                    local_path,
                    len(candidates),
                )
                return None
            # A file added to Jellyfin since the index was built is worth one rebuild; a file
            # Jellyfin does not have at all (a fresh import) must not trigger one per job.
            if refresh_index and (time.monotonic() - self._last_forced) > self.refetch_min_interval_s:
                self._last_forced = time.monotonic()
                self._ensure_index(force=True)
                return self.find_item(local_path, refresh_index=False)
        except requests.RequestException as exc:
            log.warning("jellyfin item lookup failed for %s: %s", local_path, exc)
        except Exception as exc:  # a shape we didn't expect must not fail the pipeline
            log.warning("jellyfin item lookup error for %s: %s", local_path, exc)
        return None

    # --- refresh ---------------------------------------------------------------------
    def refresh_item(self, item_id: str) -> bool:
        """Re-probe one item — the API behind the UI's "Refresh metadata". This is what actually
        picks up dropped audio/subtitle tracks; a library scan does not."""
        if not self.api_key:
            return False
        try:
            r = requests.post(
                f"{self.base_url}/Items/{item_id}/Refresh",
                headers=self._headers(),
                params={
                    "metadataRefreshMode": "FullRefresh",
                    "imageRefreshMode": "None",
                    "replaceAllMetadata": "false",
                    "replaceAllImages": "false",
                },
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("jellyfin item refresh failed (%s): %s", item_id, exc)
            return False

    def refresh_path(self, local_path: str) -> str | None:
        """Refresh the item at ``local_path``. Returns the item id on success, else None so the
        caller can fall back to a library scan."""
        item_id = self.find_item(local_path)
        if not item_id:
            return None
        return item_id if self.refresh_item(item_id) else None

    def refresh_library(self) -> bool:
        """Trigger an incremental scan of all Jellyfin libraries.

        Finds added/moved/removed files only — it will NOT notice that an existing file's tracks
        changed. Use :meth:`refresh_path` for a file the pipeline rewrote in place.
        """
        if not self.api_key:
            return False
        try:
            r = requests.post(
                f"{self.base_url}/Library/Refresh",
                headers=self._headers(),
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except requests.RequestException as exc:
            log.warning("jellyfin library refresh failed (%s): %s", self.base_url, exc)
            return False
