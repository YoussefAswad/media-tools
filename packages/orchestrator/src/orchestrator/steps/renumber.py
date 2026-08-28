"""Renumber the episode on disk and re-register it with Sonarr.

Runs AFTER prune, so it moves a file that is already in its final form.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Any

from mediacore.arr.events import ArrEvent
from mediacore.audit import FAIL, OK, SKIP
from mediacore.paths import to_remote
from mediacore.renumber.executor import execute_operations
from mediacore.renumber.models import ShowConfig
from mediacore.renumber.planner import EPISODE_PATTERN, plan_operations

from orchestrator.steps.base import JobContext, StepResult

log = logging.getLogger(__name__)


def _scan_single(file: Path):
    m = EPISODE_PATTERN.match(file.name)
    if m:
        yield file, m


class RenumberStep:
    """Sonarr only, and only for shows with configured offsets. Non-fatal: a failure here leaves
    a correctly pruned file under its original numbering, which is a worse name but not a
    broken library."""

    name = "seasonal-shift"
    silent = False

    def applies(self, ctx: JobContext) -> bool:
        return ctx.services.config.enable_seasonal_shift and ctx.event.app == "sonarr"

    def run(self, ctx: JobContext) -> StepResult:
        local_path = Path(ctx.local_path)
        show = self._matching_show(ctx, ctx.event.title)
        if show is None:
            return StepResult("seasonal-shift", "shift", SKIP, "no offset config for show")
        if not EPISODE_PATTERN.match(local_path.name):
            return StepResult("seasonal-shift", "shift", SKIP, "filename not SxxExx")
        try:
            ops = plan_operations(show, lambda _p: _scan_single(local_path))
            if not ops:
                return StepResult("seasonal-shift", "shift", SKIP, "no offset for this season")
            op = ops[0]
            # Capture the quality/release group Sonarr recorded for the ORIGINAL episode
            # before we move the file — Sonarr drops the file record once it's gone, and the
            # shifted filename carries no quality tokens to re-detect from.
            prefetch = self._sonarr_prefetch(ctx, op)
            undo_file = (
                ctx.services.config.audit.state_dir
                / "undo"
                / f"{ctx.event.item_id}-{local_path.stem}.json"
            )
            undo_file.parent.mkdir(parents=True, exist_ok=True)
            execute_operations(ops, undo_file)
            self._sonarr_import(ctx, op, prefetch)
            return StepResult(
                "seasonal-shift",
                "shift",
                OK,
                f"{op.source.name} -> {op.destination.name}",
                new_path=str(op.destination),
            )
        except Exception as exc:  # non-fatal
            log.warning("seasonal-shift failed for %s: %s", local_path, exc)
            return StepResult("seasonal-shift", "shift", FAIL, error=str(exc))

    def _matching_show(self, ctx: JobContext, title: str) -> ShowConfig | None:
        for show in ctx.services.config.shows:
            if show.name.lower() == title.lower():
                return show
        return None

    def _sonarr_prefetch(self, ctx: JobContext, op: Any) -> dict[str, Any] | None:
        """Look up the Sonarr episode id + its current quality/release group, by the ORIGINAL
        (pre-shift) season/episode — that is how Sonarr tracks the episode."""
        event: ArrEvent = ctx.event
        client = ctx.services.arr_client("sonarr")
        if client is None or event.item_id is None:
            return None
        try:
            ep_id = client.get_episode_id(event.item_id, op.season, op.episode)  # type: ignore[attr-defined]
            ef = client.get_episode_file(ep_id)  # type: ignore[attr-defined]
            return {
                "episode_id": ep_id,
                "quality": ef.get("quality") if ef else None,
                "release_group": (ef.get("releaseGroup") if ef else None),
            }
        except Exception as exc:  # non-fatal
            log.warning("sonarr prefetch failed (S%02dE%02d): %s", op.season, op.episode, exc)
            return None

    def _sonarr_import(self, ctx: JobContext, op: Any, prefetch: dict[str, Any] | None) -> None:
        """Re-register the shifted file with Sonarr under its ORIGINAL episode numbering,
        preserving the prefetched quality/release group (best-effort). Mirrors the standalone
        seasonal-shift's refresh -> manual-import flow."""
        event: ArrEvent = ctx.event
        client = ctx.services.arr_client("sonarr")
        instance = ctx.services.arr_instance("sonarr")
        if client is None or event.item_id is None:
            return
        # Let Sonarr notice the old file is gone before re-importing the shifted one.
        with contextlib.suppress(Exception):
            cmd = client.refresh(event.item_id)
            if isinstance(cmd, dict) and cmd.get("id"):
                client.wait_for_command(int(cmd["id"]))
        remote_dest = str(to_remote(instance, op.destination)) if instance else str(op.destination)
        try:
            ep_id = (
                prefetch.get("episode_id")
                if prefetch
                else client.get_episode_id(event.item_id, op.season, op.episode)  # type: ignore[attr-defined]
            )
            quality = prefetch.get("quality") if prefetch else None
            release_group = prefetch.get("release_group") if prefetch else None
            candidate = client.detect_file(  # type: ignore[attr-defined]
                event.item_id, remote_dest, op.season, ep_id,
                quality=quality, release_group=release_group,
            )
            client.import_file(event.item_id, ep_id, candidate)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("sonarr import failed for %s: %s", op.destination.name, exc)
