"""Config loading/saving with XDG discovery and ``${ENV}`` expansion.

Generic over any Pydantic model so each tool can load its own config shape while sharing the
discovery, env-expansion, and atomic-write machinery. Env expansion runs on the *loaded* data
only — the on-disk file keeps ``${VAR}`` references intact, so saving from the web UI never
leaks a resolved secret back into the file.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)

APP_NAME = "media-tools"

# ${VAR} or ${VAR:-default}
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def _expand_str(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        env = os.environ.get(name)
        if env is not None:
            return env
        return default if default is not None else ""

    return _ENV_RE.sub(repl, value)


def expand_env(data: Any) -> Any:
    """Recursively expand ``${VAR}`` references in strings within a loaded structure."""
    if isinstance(data, str):
        return _expand_str(data)
    if isinstance(data, dict):
        return {k: expand_env(v) for k, v in data.items()}
    if isinstance(data, list):
        return [expand_env(v) for v in data]
    return data


def get_config_dir() -> Path:
    xdg = os.getenv("XDG_CONFIG_HOME")
    return (Path(xdg) if xdg else Path.home() / ".config") / APP_NAME


def find_default_config() -> Path:
    d = get_config_dir()
    for name in ("config.yaml", "config.yml", "config.json"):
        p = d / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"No config file found in {d}. Expected config.yaml, config.yml, or config.json"
    )


def read_raw(path: Path) -> dict[str, Any]:
    """Parse the file without env expansion (the on-disk form, for editing)."""
    text = path.read_text()
    if path.suffix == ".json":
        return json.loads(text) or {}
    return yaml.safe_load(text) or {}


def load_config(model: type[M], path: Path) -> M:
    """Parse ``path``, expand env references, and validate into ``model``."""
    return model.model_validate(expand_env(read_raw(path)))


def parse_config(model: type[M], text: str, *, suffix: str = ".yaml") -> M:
    """Validate config from an in-memory string (used by the UI's raw editor)."""
    data = (json.loads(text) if suffix == ".json" else yaml.safe_load(text)) or {}
    return model.model_validate(expand_env(data))


def save_text(path: Path, text: str) -> None:
    """Atomically write raw config text (keeping any ``${VAR}`` references verbatim)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
