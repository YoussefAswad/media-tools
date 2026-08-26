from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Iterator

EPISODE_PATTERN = re.compile(
    r"(.+) - [Ss](\d{2})[Ee](\d{2})( - .*)?\.(.+)"
)


def scan_show(show_path: Path) -> Iterator[tuple[Path, re.Match[str]]]:
    for season_dir in sorted(show_path.iterdir()):

        if not season_dir.is_dir():
            continue

        for file in season_dir.iterdir():

            if not file.is_file():
                continue

            match = EPISODE_PATTERN.match(file.name)

            if match:
                yield file, match
