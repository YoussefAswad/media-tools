from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from .models import Config


APP_NAME: str = "seasonal-shift"


def get_config_dir() -> Path:
    xdg_config_home: str | None = os.getenv("XDG_CONFIG_HOME")

    if xdg_config_home:
        return Path(xdg_config_home) / APP_NAME

    return Path.home() / ".config" / APP_NAME


def find_default_config() -> Path:
    config_dir: Path = get_config_dir()

    yaml_path: Path = config_dir / "config.yaml"
    yml_path: Path = config_dir / "config.yml"
    json_path: Path = config_dir / "config.json"

    if yaml_path.exists():
        return yaml_path

    if yml_path.exists():
        return yml_path

    if json_path.exists():
        return json_path

    raise FileNotFoundError(
        f"No config file found in {config_dir}. "
        f"Expected config.yaml, config.yml, or config.json"
    )


def load_config(path: Path) -> Config:
    data: dict[str, Any]

    if path.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(path.read_text())
    else:
        data = json.loads(path.read_text())

    return Config.model_validate(data)
