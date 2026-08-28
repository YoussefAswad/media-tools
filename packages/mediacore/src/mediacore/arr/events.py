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
    #: the arr's ``originalLanguage`` name for the title (e.g. "Japanese"). Present on both
    #: apps' Download/Rename payloads, so the audio language filter needs no extra API call.
    original_language: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        return self.event_type in ACTIONABLE and bool(self.paths)

    @property
    def is_test(self) -> bool:
        return self.event_type == TEST

    def primary_path(self) -> str | None:
        return self.paths[0] if self.paths else None


def _original_language(item: dict[str, Any]) -> str | None:
    """The arr's original-language *name* off a movie/series block, if it sent one."""
    lang = item.get("originalLanguage") or {}
    name = lang.get("name") if isinstance(lang, dict) else lang
    return name or None


def _label_from_paths(paths: list[str], kind: str) -> str:
    """A human label for an event that carries no title.

    A re-run synthesizes a Download event from a path alone, so there is no arr title to use.
    The filename is the most informative thing available — and for an episode it already carries
    the SxxExx — which beats a notification that just says "Unknown series".
    """
    if paths:
        return paths[0].rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return f"unknown {kind}"


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_radarr(payload: dict[str, Any]) -> ArrEvent:
    event_type = payload.get("eventType", "")
    movie = payload.get("movie") or {}
    # Empty rather than a placeholder: `title` is the *arr's* title, and callers key per-title
    # policy off it. A made-up "Unknown movie" would look like a real title to them.
    title = movie.get("title") or ""
    year = movie.get("year")

    paths: list[str] = []
    mf = payload.get("movieFile") or {}
    if mf.get("path"):
        paths.append(mf["path"])
    for r in payload.get("renamedMovieFiles") or []:
        if r.get("path"):
            paths.append(r["path"])

    titled = f"{title} ({year})" if year else title
    label = titled if title else _label_from_paths(paths, "movie")

    return ArrEvent(
        app="radarr",
        event_type=event_type,
        item_id=_int(movie.get("id")),
        paths=paths,
        title=title,
        label=label,
        is_upgrade=bool(payload.get("isUpgrade")),
        download_id=(payload.get("downloadId") or None),
        original_language=_original_language(movie),
        raw=payload,
    )


def parse_sonarr(payload: dict[str, Any]) -> ArrEvent:
    event_type = payload.get("eventType", "")
    series = payload.get("series") or {}
    title = series.get("title") or ""  # see parse_radarr: empty, never a placeholder

    episodes = payload.get("episodes") or []
    season = _int(episodes[0].get("seasonNumber")) if episodes else None
    ep_nums = [n for n in (_int(e.get("episodeNumber")) for e in episodes) if n is not None]

    paths: list[str] = []
    ef = payload.get("episodeFile") or {}
    if ef.get("path"):
        paths.append(ef["path"])
    for r in payload.get("renamedEpisodeFiles") or []:
        if r.get("path"):
            paths.append(r["path"])

    if title and season is not None and ep_nums:
        eps = "".join(f"E{n:02d}" for n in ep_nums)
        label = f"{title} - S{season:02d}{eps}"
    elif title:
        label = title
    else:
        label = _label_from_paths(paths, "episode")

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
        original_language=_original_language(series),
        raw=payload,
    )


def parse_event(app: str, payload: dict[str, Any]) -> ArrEvent:
    if app == "radarr":
        return parse_radarr(payload)
    if app == "sonarr":
        return parse_sonarr(payload)
    raise ValueError(f"unknown arr app: {app!r}")
