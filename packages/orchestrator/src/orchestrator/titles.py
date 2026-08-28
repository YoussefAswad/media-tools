"""Path → title index, so a file with no webhook still knows its original language.

The import path gets ``originalLanguage`` free in the webhook payload. Everything else — the
library scan, a Library-tab re-run, a manual ``/rerun`` — starts from a path on disk and nothing
else, so it has to ask the arrs which title owns that path.

Both arrs expose every movie/series with its library folder, which makes this a longest-prefix
match. Measured against a real library it resolves 99.9% of files; the misses are folders the arr
no longer knows about (a series deleted from Sonarr with its files kept), and for those the
correct answer is "no opinion" — the audio filter then keeps every track.

Best-effort throughout: an unreachable arr yields an empty index, never an exception.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from mediacore.arr.languages import iso_for_arr_language
from mediacore.paths import to_local

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TitleInfo:
    app: str
    title: str
    #: the arr's original-language name, or None when it sent a placeholder we can't use.
    original_language: str | None
    #: ISO-639-2 codes for that language. Empty = unknown, which means "keep every audio track".
    original_isos: frozenset[str]


class TitleIndex:
    """Longest-prefix path lookup over the arrs' libraries, refreshed on a TTL."""

    def __init__(self, services, ttl_s: float = 3600.0) -> None:
        self.services = services
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._entries: list[tuple[str, TitleInfo]] = []
        self._loaded_at = 0.0

    def _fetch(self) -> list[tuple[str, TitleInfo]]:
        entries: list[tuple[str, TitleInfo]] = []
        for app in ("radarr", "sonarr"):
            client = self.services.arr_client(app)
            instance = self.services.arr_instance(app)
            if client is None:
                continue
            try:
                items = client.titles()  # type: ignore[attr-defined]
            except Exception as exc:  # unreachable arr must not break a scan
                log.warning("title index: %s unreachable: %s", app, exc)
                continue
            for item in items:
                path = item.get("path")
                if not path:
                    continue
                name = (item.get("originalLanguage") or {}).get("name")
                isos = iso_for_arr_language(name)
                local = str(to_local(instance, path)) if instance else str(path)
                entries.append(
                    (
                        local.rstrip("/") + "/",
                        TitleInfo(
                            app=app,
                            title=item.get("title") or "",
                            original_language=name if isos else None,
                            original_isos=frozenset(isos),
                        ),
                    )
                )
        # longest prefix first, so a nested folder beats its parent
        entries.sort(key=lambda e: len(e[0]), reverse=True)
        return entries

    def refresh(self) -> int:
        """Rebuild now. Returns the entry count; keeps the previous index if the arrs gave us
        nothing, so a transient outage doesn't turn every lookup into 'unknown'."""
        entries = self._fetch()
        with self._lock:
            if entries or not self._entries:
                self._entries = entries
            self._loaded_at = time.monotonic()
            return len(self._entries)

    def _ensure_fresh(self) -> None:
        if not self._entries or (time.monotonic() - self._loaded_at) > self.ttl_s:
            self.refresh()

    def lookup(self, local_path: str) -> TitleInfo | None:
        """The title owning ``local_path``, or None when no arr claims it."""
        self._ensure_fresh()
        with self._lock:
            entries = self._entries
        for prefix, info in entries:
            if local_path.startswith(prefix):
                return info
        return None

    def original_isos(self, local_path: str, title: str | None = None) -> set[str] | None:
        """ISO codes to treat as this file's original language, honouring the per-title override.

        ``None`` means "unknown" — the audio filter keeps every track. An override wins over the
        arr's own answer, which is the escape hatch for titles whose production origin isn't the
        audio anyone wants (an English-original anime anthology, say).
        """
        info = self.lookup(local_path)
        # The index's own title is authoritative. A caller-supplied one is only a fallback for a
        # path no arr claims: a re-run synthesizes its event from a path, so whatever title it
        # carries would otherwise shadow the real one and silently disable the override.
        name = (info.title if info else None) or title
        override = override_for(self.services.config.policy, name)
        if override is not None:
            return override
        if info is None:
            return None
        return set(info.original_isos) or None


def override_for(policy, title: str | None) -> set[str] | None:
    """Per-title override from the policy, matched case-insensitively on the arr title."""
    if not title or not policy.audio_original_overrides:
        return None
    wanted = title.strip().lower()
    for key, codes in policy.audio_original_overrides.items():
        if key.strip().lower() == wanted:
            return {c.lower() for c in codes}
    return None
