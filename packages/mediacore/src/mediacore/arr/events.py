"""Parse Radarr/Sonarr *Connect → Webhook* payloads into a normalized :class:`ArrEvent`.

Webhook payloads vary across arr versions, so parsing is defensive (``dict.get``) rather than a
strict schema. We only extract what the dispatch pipeline needs: the app, the event type, the
item id (for rescans), the imported/renamed file path, a human label, and — for Sonarr — the
season/episode numbers seasonal-shift keys on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Event types we act on (others are ignored). Import/upgrade both arrive as "Download".
ACTIONABLE = {"Download", "Rename"}
TEST = "Test"


@dataclass
class ArrEvent:
    app: str  # "radarr" | "sonarr"
    event_type: str
    item_id: int | None  # movie id / series id, for rescan commands
    paths: list[str]  # arr-reported path(s) of the imported/renamed file(s)
    title: str
    label: str  # e.g. "Movie (2021)" or "Show — S02E03"
    season: int | None = None
    episodes: list[int] = field(default_factory=list)
    is_upgrade: bool = False
    download_id: str | None = None  # torrent infohash (arr "downloadId"), for the bandwidth ledger
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.event_type in ACTIONABLE and bool(self.paths)

    @property
    def is_test(self) -> bool:
        return self.event_type == TEST

    def primary_path(self) -> str | None:
        return self.paths[0] if self.paths else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_radarr(payload: dict[str, Any]) -> ArrEvent:
    event_type = payload.get("eventType", "")
    movie = payload.get("movie") or {}
    title = movie.get("title") or "Unknown movie"
    year = movie.get("year")
    label = f"{title} ({year})" if year else title

    paths: list[str] = []
    mf = payload.get("movieFile") or {}
    if mf.get("path"):
        paths.append(mf["path"])
    for r in payload.get("renamedMovieFiles") or []:
        if r.get("path"):
            paths.append(r["path"])

    return ArrEvent(
        app="radarr",
        event_type=event_type,
        item_id=_int(movie.get("id")),
        paths=paths,
        title=title,
        label=label,
        is_upgrade=bool(payload.get("isUpgrade")),
        download_id=(payload.get("downloadId") or None),
        raw=payload,
    )


def parse_sonarr(payload: dict[str, Any]) -> ArrEvent:
    event_type = payload.get("eventType", "")
    series = payload.get("series") or {}
    title = series.get("title") or "Unknown series"

    episodes = payload.get("episodes") or []
    season = _int(episodes[0].get("seasonNumber")) if episodes else None
    ep_nums = [n for n in (_int(e.get("episodeNumber")) for e in episodes) if n is not None]
    if season is not None and ep_nums:
        eps = "".join(f"E{n:02d}" for n in ep_nums)
        label = f"{title} - S{season:02d}{eps}"
    else:
        label = title

    paths: list[str] = []
    ef = payload.get("episodeFile") or {}
    if ef.get("path"):
        paths.append(ef["path"])
    for r in payload.get("renamedEpisodeFiles") or []:
        if r.get("path"):
            paths.append(r["path"])

    return ArrEvent(
        app="sonarr",
        event_type=event_type,
        item_id=_int(series.get("id")),
        paths=paths,
        title=title,
        label=label,
        season=season,
        episodes=ep_nums,
        is_upgrade=bool(payload.get("isUpgrade")),
        download_id=(payload.get("downloadId") or None),
        raw=payload,
    )


def parse_event(app: str, payload: dict[str, Any]) -> ArrEvent:
    if app == "radarr":
        return parse_radarr(payload)
    if app == "sonarr":
        return parse_sonarr(payload)
    raise ValueError(f"unknown arr app: {app!r}")
