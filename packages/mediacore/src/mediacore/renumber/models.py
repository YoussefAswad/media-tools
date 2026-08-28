from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class SeasonConfig(BaseModel):
    season_offset: int = 0
    episode_offset: int = 0


class ShowConfig(BaseModel):
    name: str
    path: Path
    seasons: dict[int, SeasonConfig] = {}
    specials: dict[int, int] = {}


class FileOperation(BaseModel):
    """One planned rename.

    ``season``/``episode`` are the numbers parsed from the *source* filename; only
    ``destination`` carries the shifted ones. Sonarr tracks the episode by its original
    numbering, so the re-import after the move keys on these, not on the destination.
    """

    source: Path
    destination: Path
    season: int
    episode: int


class UndoEntry(BaseModel):
    source: Path
    destination: Path
