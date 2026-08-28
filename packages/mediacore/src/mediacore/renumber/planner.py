from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from pathlib import Path

from mediacore.renumber.models import FileOperation, ShowConfig

#: The only filename shape this understands: ``Show - S01E02[ - Title].ext``.
#: Two-digit season/episode only — multi-episode files (``S01E01-E02``) and >99-episode seasons
#: do not match and are skipped rather than mis-numbered.
EPISODE_PATTERN = re.compile(r"(.+) - [Ss](\d{2})[Ee](\d{2})( - .*)?\.(.+)")


def plan_operations(
    show: ShowConfig,
    scanner: Callable[[Path], Iterable[tuple[Path, re.Match[str]]]],
) -> list[FileOperation]:

    operations: list[FileOperation] = []

    for file, match in scanner(show.path):

        show_name, season, episode, ep_name_raw, ext = match.groups()

        season_int: int = int(season)
        episode_int: int = int(episode)

        if season_int == 0:
            if episode_int not in show.specials:
                continue
            new_season: int = 0
            new_episode: int = show.specials[episode_int]
        elif season_int not in show.seasons:
            continue
        else:
            config = show.seasons[season_int]
            new_season = season_int + config.season_offset
            new_episode = episode_int + config.episode_offset
            if new_episode < 1:
                continue

        # ep_name_raw is the regex group " - <title>"; drop that leading separator (not
        # arbitrary space/dash chars, which would corrupt a title that itself starts with one).
        episode_name: str | None = ep_name_raw.removeprefix(" - ").strip() if ep_name_raw else None

        if episode_name:
            new_name: str = (
                f"{show_name} - S{new_season:02d}E{new_episode:02d} - {episode_name}.{ext}"
            )
        else:
            new_name = f"{show_name} - S{new_season:02d}E{new_episode:02d}.{ext}"

        new_dir: Path = show.path / ("Specials" if new_season == 0 else f"Season {new_season}")
        new_path: Path = new_dir / new_name

        operations.append(
            FileOperation(
                source=file,
                destination=new_path,
                season=season_int,
                episode=episode_int,
            )
        )

    return operations
