from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich import print

from seasonal_shift.models import Config, FileOperation, ShowConfig, SonarrConfig, UndoEntry

from .cleanup import cleanup_shows
from .config import find_default_config, load_config
from .doctor import run_doctor
from .executor import execute_operations, find_latest_undo_file, get_default_undo_file
from .planner import detect_collisions, detect_duplicates, plan_operations
from .preview import show_preview
from .scanner import scan_show
from .sonarr import QualityWrapper, SonarrClient
from .undo import run_undo
from .watcher import run_watch

app = typer.Typer()


@dataclass
class _EpisodePrefetch:
    series_id: int
    episode_id: int
    quality: QualityWrapper | None = None
    release_group: str | None = None


@app.command()
def run(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", "-c", help="Config file (defaults to XDG config directory)"
        ),
    ] = None,
    undo_file: Annotated[
        Path | None,
        typer.Option(
            "--undo-file", help="Undo log file (defaults to XDG state directory)"
        ),
    ] = None,
) -> None:
    """
    Apply episode shifts defined in config.
    """

    if config is None:
        config = find_default_config()

    cfg = load_config(config)

    all_operations: list[FileOperation] = []
    show_operations: list[tuple[ShowConfig, list[FileOperation]]] = []

    for show in cfg.shows:

        operations = plan_operations(show, scan_show)

        show_preview(show, operations)

        show_operations.append((show, operations))
        all_operations.extend(operations)

    collisions = detect_collisions(all_operations)

    if collisions:
        print("[red]Collision detected:[/]")
        for c in collisions:
            print(c)
        raise typer.Exit(1)

    duplicates = detect_duplicates(all_operations)

    if duplicates:
        print("[yellow]Duplicate destination detected:[/]")
        for a, b in duplicates:
            print(a, b)

    if not typer.confirm("Apply these changes?"):
        raise typer.Exit()

    if undo_file is None:
        undo_file = get_default_undo_file()

    sonarr_client: SonarrClient | None = None
    prefetch: dict[tuple[str, int, int], _EpisodePrefetch] = {}
    if cfg.sonarr:
        sonarr_client = SonarrClient(str(cfg.sonarr.base_url), cfg.sonarr.api_key)
        prefetch = _sonarr_prefetch(sonarr_client, show_operations)

    execute_operations(all_operations, undo_file)

    removed = cleanup_shows(show.path for show in cfg.shows)

    if removed:
        print(f"[blue]Removed empty folders:[/] {len(removed)}")

    print(f"[green]Done.[/] Undo file saved to: {undo_file}")

    if cfg.sonarr and sonarr_client:
        _sonarr_update(sonarr_client, cfg.sonarr, show_operations, prefetch)


def _to_sonarr_path(path: Path, local_root: Path, sonarr_root: Path) -> Path:
    return sonarr_root / path.relative_to(local_root)


def _sonarr_prefetch(
    client: SonarrClient,
    show_operations: list[tuple[ShowConfig, list[FileOperation]]],
) -> dict[tuple[str, int, int], _EpisodePrefetch]:
    result: dict[tuple[str, int, int], _EpisodePrefetch] = {}
    for show, operations in show_operations:
        try:
            series_id, _ = client.get_series(show.name)
        except Exception:
            continue
        for op in operations:
            try:
                ep_id = client.get_episode_id(series_id, op.season, op.episode)
                ep_file = client.get_episode_file(ep_id)
                if ep_file is None:
                    refresh = client.refresh_series(series_id)
                    client.wait_for_command(refresh["id"])
                    ep_file = client.get_episode_file(ep_id)
                prefetch = _EpisodePrefetch(series_id=series_id, episode_id=ep_id)
                if ep_file:
                    prefetch.quality = ep_file["quality"]
                    prefetch.release_group = ep_file.get("releaseGroup") or None
                result[(show.name, op.season, op.episode)] = prefetch
            except Exception as e:
                print(
                    f"[yellow]Sonarr prefetch failed S{op.season:02d}E{op.episode:02d}: {e}[/]"
                )
    return result


def _sonarr_update(
    client: SonarrClient,
    sonarr_cfg: SonarrConfig,
    show_operations: list[tuple[ShowConfig, list[FileOperation]]],
    prefetch: dict[tuple[str, int, int], _EpisodePrefetch],
) -> None:
    for show, operations in show_operations:
        if not operations:
            continue

        try:
            series_id, _ = client.get_series(show.name)
        except Exception as e:
            print(f"[yellow]Sonarr: could not find series '{show.name}': {e}[/]")
            continue

        try:
            refresh = client.refresh_series(series_id)
            client.wait_for_command(refresh["id"])
        except Exception as e:
            print(f"[yellow]Sonarr: rescan failed for '{show.name}': {e}[/]")

        for op in operations:
            try:
                sonarr_dest = (
                    _to_sonarr_path(
                        op.destination,
                        sonarr_cfg.local_shows_root,
                        sonarr_cfg.shows_root,
                    )
                    if sonarr_cfg.shows_root and sonarr_cfg.local_shows_root
                    else op.destination
                )
                info = prefetch.get((show.name, op.season, op.episode))
                ep_id = info.episode_id if info else client.get_episode_id(series_id, op.season, op.episode)
                quality = info.quality if info else None
                release_group = info.release_group if info else None
                candidate = client.detect_file(
                    series_id, sonarr_dest, op.season, ep_id,
                    quality=quality, release_group=release_group,
                )
                client.import_file(series_id, ep_id, candidate)
                print(
                    f"[green]Sonarr:[/] imported S{op.season:02d}E{op.episode:02d}"
                    f" → {op.destination.name}"
                )
            except Exception as e:
                print(
                    f"[yellow]Sonarr: failed S{op.season:02d}E{op.episode:02d}"
                    f" ({op.destination.name}): {e}[/]"
                )


@app.command()
def undo(
    undo_file: Annotated[
        Path | None,
        typer.Argument(help="Undo file (defaults to latest operation)"),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", "-c", help="Config file (defaults to XDG config directory)"
        ),
    ] = None,
) -> None:
    """
    Undo the latest rename operation.
    """

    if undo_file is None:
        undo_file = find_latest_undo_file()

        if undo_file is None:
            print("[red]No undo files found.[/]")
            raise typer.Exit(1)

        print(f"[blue]Using latest undo file:[/] {undo_file}")

    run_undo(undo_file)

    print("[green]Undo complete[/]")

    if config is None:
        config = find_default_config()

    cfg = load_config(config)

    if cfg.sonarr:
        client = SonarrClient(str(cfg.sonarr.base_url), cfg.sonarr.api_key)
        _sonarr_refresh_after_undo(client, undo_file, cfg.shows)


def _sonarr_refresh_after_undo(
    client: SonarrClient,
    undo_file: Path,
    cfg_shows: list[ShowConfig],
) -> None:
    import json

    entries = [UndoEntry.model_validate(e) for e in json.loads(undo_file.read_text())]

    affected: set[str] = set()
    for entry in entries:
        for show in cfg_shows:
            try:
                entry.destination.relative_to(show.path)
                affected.add(show.name)
                break
            except ValueError:
                pass

    for show_name in affected:
        try:
            series_id, _ = client.get_series(show_name)
            refresh = client.refresh_series(series_id)
            client.wait_for_command(refresh["id"])
            print(f"[green]Sonarr:[/] refreshed '{show_name}'")
        except Exception as e:
            print(f"[yellow]Sonarr: failed to refresh '{show_name}': {e}[/]")


@app.command()
def doctor(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", "-c", help="Config file (defaults to XDG config directory)"
        ),
    ] = None,
) -> None:
    """
    Run diagnostics on the library and configuration.
    """

    if config is None:
        config = find_default_config()

    cfg = load_config(config)

    run_doctor(cfg.shows)


def _sonarr_process(
    client: SonarrClient,
    sonarr_cfg: SonarrConfig,
    show: ShowConfig,
    ops: list[FileOperation],
) -> None:
    show_ops = [(show, ops)]
    prefetch = _sonarr_prefetch(client, show_ops)
    _sonarr_update(client, sonarr_cfg, show_ops, prefetch)


@app.command()
def watch(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config file (defaults to XDG config directory)"),
    ] = None,
) -> None:
    """
    Watch show directories and auto-apply episode shifts on new files.
    """
    if config is None:
        config = find_default_config()

    def sonarr_cb_factory(cfg: Config) -> Callable[[ShowConfig, list[FileOperation]], None] | None:
        if not cfg.sonarr:
            return None
        client = SonarrClient(str(cfg.sonarr.base_url), cfg.sonarr.api_key)
        sonarr_cfg = cfg.sonarr
        return lambda show, ops: _sonarr_process(client, sonarr_cfg, show, ops)  # noqa: E731

    run_watch(config, sonarr_cb_factory)
