from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, model_validator


class SonarrConfig(BaseModel):
    base_url: str
    api_key: str
    shows_root: Path | None = None
    local_shows_root: Path | None = None

    @model_validator(mode="after")
    def check_path_map_consistency(self) -> SonarrConfig:
        if (self.shows_root is None) != (self.local_shows_root is None):
            raise ValueError(
                "shows_root and local_shows_root must both be set or both omitted"
            )
        return self


class SeasonConfig(BaseModel):
    season_offset: int = 0
    episode_offset: int = 0


class ShowConfig(BaseModel):
    name: str
    path: Path
    seasons: dict[int, SeasonConfig] = {}
    specials: dict[int, int] = {}


class Config(BaseModel):
    shows: list[ShowConfig]
    sonarr: SonarrConfig | None = None


class FileOperation(BaseModel):
    source: Path
    destination: Path
    season: int
    episode: int


class UndoEntry(BaseModel):
    source: Path
    destination: Path
