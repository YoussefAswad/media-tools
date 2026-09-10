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

APP_NAME = "media-orchestrator"
#: The pre-rename directory name. Still searched, because APP_NAME picks an on-disk path: a
#: checkout that already had ~/.config/media-tools/config.yaml would otherwise stop finding its
#: config with no message beyond "no config file found".
LEGACY_APP_NAMES = ("media-tools",)

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


def _config_root() -> Path:
    xdg = os.getenv("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def get_config_dir() -> Path:
    """Where a new config would be written."""
    return _config_root() / APP_NAME


def config_dirs() -> list[Path]:
    """Directories searched for a config, current name first."""
    root = _config_root()
    return [root / APP_NAME] + [root / name for name in LEGACY_APP_NAMES]


def find_default_config() -> Path:
    """The first config found under any searched directory. The container never gets here —
    it sets ``ORCHESTRATOR_CONFIG`` explicitly — so this is the standalone/dev path."""
    for d in config_dirs():
        for name in ("config.yaml", "config.yml", "config.json"):
            p = d / name
            if p.exists():
                return p
    searched = ", ".join(str(d) for d in config_dirs())
    raise FileNotFoundError(
        f"No config file found in {searched}. Expected config.yaml, config.yml, or config.json"
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


def replace_mapping_block(text: str, section: str, key: str, mapping: dict[str, str]) -> str:
    """Rewrite one ``key:`` mapping nested under one top-level ``section:``, in place.

    A surgical text edit, deliberately, because the obvious alternative is not available: dumping
    the validated model back to YAML would resolve every ``${VAR}`` reference into the file and
    flatten every comment in it, which is exactly what
    :mod:`orchestrator.config_schema` refuses to let the UI do. Rewriting only the lines of one
    flat ``name: value`` block leaves every other byte — including every secret reference —
    untouched.

    The block is created if the section exists without it, and the section is created if it does
    not exist at all. **Comments inside the target block are not preserved** (nothing else is
    touched); that is the whole cost of letting the UI edit this one map.

    Values are quoted, so a device called ``No`` or ``12:34`` cannot turn into a bool or a
    sexagesimal int on the next load — YAML's classic own goal.
    """
    lines = text.splitlines()
    body = [f'  {key}:'] + [f'    "{k}": "{v}"' for k, v in mapping.items()]
    if not mapping:
        body = [f'  {key}: {{}}']

    sec_at = next(
        (i for i, ln in enumerate(lines) if ln.rstrip() == f"{section}:" or ln.startswith(f"{section}:")),
        None,
    )
    if sec_at is None:
        head = (text.rstrip("\n") + "\n\n") if text.strip() else ""
        return head + f"{section}:\n" + "\n".join(body) + "\n"

    # The section runs until the next line that starts in column 0 and is not blank or a comment.
    sec_end = len(lines)
    for i in range(sec_at + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and not ln[0].isspace() and not ln.lstrip().startswith("#"):
            sec_end = i
            break

    key_at = next(
        (
            i
            for i in range(sec_at + 1, sec_end)
            if lines[i].startswith("  ") and lines[i].strip().split(":")[0] == key
            and len(lines[i]) - len(lines[i].lstrip()) == 2
        ),
        None,
    )
    if key_at is None:
        insert = sec_end
        while insert > sec_at + 1 and not lines[insert - 1].strip():
            insert -= 1
        return "\n".join(lines[:insert] + body + lines[insert:]) + "\n"

    # The existing block ends at the next line indented two spaces or less that carries content.
    key_end = sec_end
    for i in range(key_at + 1, sec_end):
        ln = lines[i]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= 2:
            key_end = i
            break
    return "\n".join(lines[:key_at] + body + lines[key_end:]) + "\n"
