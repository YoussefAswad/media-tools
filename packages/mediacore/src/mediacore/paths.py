"""Local↔remote path translation for a media library mounted at different prefixes.

Radarr/Sonarr report paths under *their* mount (e.g. ``/data/movies/…``); this container may
mount the same library elsewhere (e.g. ``/media/movies/…``). Translate in both directions when
a root pair is configured; otherwise paths pass through unchanged (identical mounts).
"""

from __future__ import annotations

from pathlib import Path

from mediacore.models import ArrInstance


def _rebase(path: Path, from_root: Path, to_root: Path) -> Path:
    try:
        rel = path.relative_to(from_root)
    except ValueError:
        return path  # not under the configured root; leave as-is
    return to_root / rel


def to_local(instance: ArrInstance, path: str | Path) -> Path:
    """Map an arr-reported path to this container's filesystem."""
    p = Path(path)
    if instance.media_root is None or instance.local_media_root is None:
        return p
    return _rebase(p, instance.media_root, instance.local_media_root)


def to_remote(instance: ArrInstance, path: str | Path) -> Path:
    """Map a local path back to what the arr expects (e.g. for manual-import calls)."""
    p = Path(path)
    if instance.media_root is None or instance.local_media_root is None:
        return p
    return _rebase(p, instance.local_media_root, instance.media_root)
