"""The per-job dispatch pipeline.

For one imported/renamed file, in order: flag quality issues (quality-check → ntfy), prune
foreign image subs + re-encode audio (mkvprune) on the freshly-imported path, then optionally
renumber it (seasonal-shift, which moves the pruned file + re-imports to Sonarr), snapshot its
bandwidth into the ledger, and emit a status rollup. quality-check/prune run before the shift so
they always operate on the stable imported path. Each step is recorded to the audit log;
seasonal-shift and quality-check failures are non-fatal, a prune failure fails the job (retried).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mediacore.arr.events import ArrEvent, parse_event
from mediacore.audit import FAIL, OK, SKIP, ActionRecord
from mediacore.notify import PRIORITY_FAILURE, PRIORITY_FINDING, PRIORITY_STATUS, NtfyMessage
from mediacore.paths import to_local, to_remote
from mediacore.probe import ProbeResult
from mediacore.probe import probe as _default_probe
from quality_check.prune import build_mkvprune_argv, decide
from quality_check.rules import check
from seasonal_shift.executor import execute_operations
from seasonal_shift.models import ShowConfig
from seasonal_shift.planner import plan_operations
from seasonal_shift.scanner import EPISODE_PATTERN

from orchestrator.runner import PruneResult, run_mkvprune
from orchestrator.services import Services

log = logging.getLogger(__name__)


class DispatchError(RuntimeError):
    """Fatal, retryable failure (fails the job)."""


class InsufficientSpaceError(DispatchError):
    """Not enough free disk space to prune this file. The worker defers the job (long backoff,
    no attempt consumed) rather than dead-lettering — space will free up on its own (e.g. a
    torrent is cleaned up), so the job should wait, not give up."""


#: substring mkvprune emits when its own scratch-space precheck (replace.check_free_space) fails.
_LOW_SPACE_MARKER = "insufficient free space"


@dataclass
class StepResult:
    tool: str
    action: str
    result: str  # OK | SKIP | FAIL
    detail: str = ""
    error: str | None = None
    duration_ms: int | None = None  # wall time the step ran until completion


@dataclass
class DispatchOutcome:
    label: str
    steps: list[StepResult] = field(default_factory=list)
    final_local_path: str | None = None


ProbeFn = Callable[[str], ProbeResult]
PruneRunner = Callable[..., PruneResult]


class Dispatcher:
    def __init__(
        self,
        services: Services,
        *,
        probe_fn: ProbeFn | None = None,
        prune_runner: PruneRunner | None = None,
    ) -> None:
        self.services = services
        self.probe_fn: ProbeFn = probe_fn or _default_probe
        self.prune_runner: PruneRunner = prune_runner or run_mkvprune

    # --- helpers --------------------------------------------------------------------
    def _event_from_job(self, payload: dict[str, Any]) -> ArrEvent:
        app = payload["app"]
        return parse_event(app, payload["payload"])

    def _record(self, job_id: str, event: ArrEvent, step: StepResult, path: str | None) -> None:
        cfg = self.services.config
        mtime = None
        if path and os.path.exists(path):
            mtime = os.path.getmtime(path)
        self.services.audit.record(
            ActionRecord(
                tool=step.tool,
                action=step.action,
                result=step.result,
                app=event.app,
                event_type=event.event_type,
                path=path,
                mtime=mtime,
                after={"detail": step.detail} if step.detail else None,
                error=step.error,
                job_id=job_id,
                duration_ms=step.duration_ms,
            )
        )
        if cfg.ntfy.status_granularity == "action" and step.result != SKIP:
            self._notify_step(event, step)

    def _notify_step(self, event: ArrEvent, step: StepResult) -> None:
        pri = PRIORITY_FAILURE if step.result == FAIL else PRIORITY_STATUS
        tag = "rotating_light" if step.result == FAIL else "gear"
        self.services.notifier.send(
            NtfyMessage(
                title=f"{event.label} — {step.tool}",
                body=f"**{step.action}**: {step.result}\n\n{step.detail or step.error or ''}",
                tags=[event.app, tag],
                priority=pri,
            )
        )

    # --- pipeline -------------------------------------------------------------------
    def dispatch(
        self, job_id: str, payload: dict[str, Any], *, set_step=None, set_progress=None
    ) -> DispatchOutcome:
        cfg = self.services.config
        event = self._event_from_job(payload)
        outcome = DispatchOutcome(label=event.label)

        instance = self.services.arr_instance(event.app)
        remote_path = event.primary_path()
        if remote_path is None:
            raise DispatchError("event has no file path")
        local_path = str(to_local(instance, remote_path)) if instance else remote_path

        # 1. bandwidth ledger FIRST — record consumed bandwidth from qBittorrent up front, so it
        # is captured even if a later step fails (prune raising, torrent later deleted, etc.).
        # Independent of the file; idempotent (upsert by infohash).
        if cfg.enable_ledger:
            self._ledger_capture(job_id, event)

        # quality-check + prune run on the stable freshly-imported path, BEFORE seasonal-shift
        # moves/re-imports the file. mkvprune replaces in place, so the path is unchanged for the
        # shift that follows.
        # 2. quality-check ----------------------------------------------------------
        pr: ProbeResult | None = None
        if cfg.enable_quality_check:
            if set_step:
                set_step("quality-check")
            t0 = time.monotonic()
            step, pr = self._quality_step(event, local_path)
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            outcome.steps.append(step)
            self._record(job_id, event, step, local_path)

        # 3. prune (live progress via set_progress) ---------------------------------
        pruned_ran = False
        if cfg.enable_prune:
            if set_step:
                set_step("prune")
            t0 = time.monotonic()
            step = self._prune_step(event, local_path, pr, set_progress=set_progress)
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            outcome.steps.append(step)
            self._record(job_id, event, step, local_path)
            pruned_ran = True  # reached only if prune didn't raise (ok/skip)
            # A cross-format remux (mp4→mkv) writes a sibling .mkv and removes the source, so the
            # rest of the pipeline (seasonal-shift, jellyfin, final_local_path) must follow the new
            # path. Self-verifying: only reassign when the .mkv exists and the source is gone.
            new_local = os.path.splitext(local_path)[0] + ".mkv"
            if new_local != local_path and os.path.exists(new_local) and not os.path.exists(local_path):
                local_path = new_local

        # 4. seasonal-shift (moves the now-pruned file to its final numbering) -------
        if cfg.enable_seasonal_shift and event.app == "sonarr":
            if set_step:
                set_step("seasonal-shift")
            t0 = time.monotonic()
            step, new_local = self._shift_step(event, Path(local_path))
            step.duration_ms = int((time.monotonic() - t0) * 1000)
            outcome.steps.append(step)
            self._record(job_id, event, step, local_path)
            if new_local is not None:
                local_path = str(new_local)

        outcome.final_local_path = local_path

        # 5. Jellyfin refresh — after pruning (ok or skip), regardless of whether the file
        # changed, so Jellyfin picks up the (re)imported/altered file. Best-effort.
        if pruned_ran or not cfg.enable_prune:
            self._jellyfin_refresh(job_id, event, local_path)

        # 6. status rollup ----------------------------------------------------------
        self._status_rollup(event, outcome)
        return outcome

    def _jellyfin_refresh(self, job_id: str, event: ArrEvent, local_path: str | None) -> None:
        """Make Jellyfin re-read the file the pipeline just changed (best-effort, never fatal).

        A library scan only notices files appearing and disappearing, so a file rewritten in
        place keeps its stale track list forever. Refresh that one item instead, and fall back to
        a scan only when the path can't be resolved — a brand-new import, or one the pipeline
        renamed, which is the case a scan is genuinely for.
        """
        jf = self.services.jellyfin
        if jf is None:
            return
        detail, ok = "not configured / failed", False
        try:
            item_id = jf.refresh_path(local_path) if local_path else None
            if item_id:
                ok, detail = True, f"metadata refreshed (item {item_id})"
            else:
                ok = jf.refresh_library()
                detail = "library scan triggered (item not found)" if ok else detail
        except Exception as exc:
            log.warning("jellyfin refresh error: %s", exc)
        self.services.audit.record(
            ActionRecord(
                tool="jellyfin", action="refresh", result=OK if ok else SKIP,
                app=event.app, event_type=event.event_type, path=local_path, job_id=job_id,
                after={"detail": detail},
            )
        )

    def _ledger_capture(self, job_id: str, event: ArrEvent) -> None:
        """Snapshot this import's consumed bandwidth from qBittorrent into the ledger.

        Best-effort and silent: records an audit row for visibility but never a pipeline step
        (so the ntfy rollup is unchanged) and never raises.
        """
        qbit = self.services.qbit
        if qbit is None or not event.download_id:
            return
        try:
            t = qbit.torrent_by_hash(event.download_id)
        except Exception as exc:
            log.warning("ledger: qbit lookup failed: %s", exc)
            t = None
        if not t:
            self.services.audit.record(
                ActionRecord(
                    tool="ledger", action="record", result=SKIP, app=event.app,
                    event_type=event.event_type, job_id=job_id,
                    after={"detail": "torrent not found in qBittorrent"},
                )
            )
            return
        kind = "movie" if event.app == "radarr" else "episode"
        self.services.ledger.record_auto(
            infohash=event.download_id,
            name=t["name"] or event.label,
            kind=kind,
            app=event.app,
            downloaded_bytes=t["downloaded"],
            uploaded_bytes=t["uploaded"],
            size_bytes=t["total_size"],
            completion_on=t["completion_on"],
        )
        from orchestrator.util import human_bytes

        detail = f"↓{human_bytes(t['downloaded'])} ↑{human_bytes(t['uploaded'])}"
        self.services.audit.record(
            ActionRecord(
                tool="ledger", action="record", result=OK, app=event.app,
                event_type=event.event_type, job_id=job_id,
                after={"detail": detail},
            )
        )

    def _shift_step(self, event: ArrEvent, local_path: Path) -> tuple[StepResult, Path | None]:
        show = self._matching_show(event.title)
        if show is None:
            return StepResult("seasonal-shift", "shift", SKIP, "no offset config for show"), None
        if not EPISODE_PATTERN.match(local_path.name):
            return StepResult("seasonal-shift", "shift", SKIP, "filename not SxxExx"), None
        try:
            ops = plan_operations(show, lambda _p: _scan_single(local_path))
            if not ops:
                return StepResult("seasonal-shift", "shift", SKIP, "no offset for this season"), None
            op = ops[0]
            # Capture the quality/release group Sonarr recorded for the ORIGINAL episode
            # before we move the file — Sonarr drops the file record once it's gone, and the
            # shifted filename carries no quality tokens to re-detect from.
            prefetch = self._sonarr_prefetch(event, op)
            undo_file = (
                self.services.config.audit.state_dir / "undo" / f"{event.item_id}-{local_path.stem}.json"
            )
            undo_file.parent.mkdir(parents=True, exist_ok=True)
            execute_operations(ops, undo_file)
            self._sonarr_import(event, op, prefetch)
            return (
                StepResult(
                    "seasonal-shift",
                    "shift",
                    OK,
                    f"{op.source.name} -> {op.destination.name}",
                ),
                op.destination,
            )
        except Exception as exc:  # non-fatal
            log.warning("seasonal-shift failed for %s: %s", local_path, exc)
            return StepResult("seasonal-shift", "shift", FAIL, error=str(exc)), None

    def _quality_step(self, event: ArrEvent, local_path: str) -> tuple[StepResult, ProbeResult | None]:
        try:
            pr = self.probe_fn(local_path)
        except Exception as exc:
            log.warning("probe failed for %s: %s", local_path, exc)
            return StepResult("quality-check", "probe", FAIL, error=str(exc)), None
        findings = check(pr, self.services.config.policy)
        if not findings:
            return StepResult("quality-check", "check", OK, "no findings"), pr
        body = "\n".join(f"**Rule {f.rule}** — {f.title}\n" + "\n".join(f"- {d}" for d in f.details) for f in findings)
        self.services.notifier.send(
            NtfyMessage(
                title=f"{event.label}",
                body=body,
                tags=[event.app, "warning"],
                priority=PRIORITY_FINDING,
                topic=self.services.config.ntfy.topic,
            )
        )
        # Human-readable finding titles (not cryptic rule letters).
        titles = "; ".join(f.title for f in findings)
        return StepResult("quality-check", "check", OK, f"findings: {titles}"), pr

    def _prune_step(
        self, event: ArrEvent, local_path: str, pr: ProbeResult | None, *, set_progress=None
    ) -> StepResult:
        try:
            if pr is None:
                pr = self.probe_fn(local_path)
        except Exception as exc:
            return StepResult("mkvprune", "prune", FAIL, error=f"probe: {exc}")

        decision = decide(pr, self.services.config.policy)
        if not decision.needs_work:
            return StepResult("mkvprune", "prune", SKIP, "nothing to prune/re-encode")

        mtime = os.path.getmtime(local_path) if os.path.exists(local_path) else None
        if self.services.audit.already_done(local_path, "prune", mtime):
            return StepResult("mkvprune", "prune", SKIP, "already pruned (same mtime)")

        # Self-heal: a prune killed mid-mux (e.g. a container restart) leaves a stale
        # {stem}.tmp.mkv that mkvprune refuses to overwrite. Safe to remove — the worker is
        # single-threaded, so nothing else is writing it.
        stale = os.path.splitext(local_path)[0] + ".tmp.mkv"
        if os.path.exists(stale):
            with contextlib.suppress(OSError):
                os.remove(stale)
                log.info("removed stale temp before prune: %s", stale)

        self._ensure_free_space(local_path)

        argv = build_mkvprune_argv(
            local_path, decision, executable=self.services.config.mkvprune_executable
        )
        argv.insert(1, "--progress-json")  # after the executable
        on_progress = None
        if set_progress is not None:
            on_progress = lambda pct, note: set_progress(pct, note)  # noqa: E731
        result = self.prune_runner(argv, on_progress=on_progress)
        if not result.ok:
            tail = (result.stderr or result.stdout or "").strip()[-400:]
            # mkvprune runs its own scratch-space precheck; classify that shortfall as deferrable
            # (don't dead-letter for space) even though it surfaced as a subprocess exit.
            if _LOW_SPACE_MARKER in tail.lower():
                raise InsufficientSpaceError(f"mkvprune: {tail}")
            raise DispatchError(f"mkvprune exit {result.returncode}: {tail}")

        self._rescan(event)
        return StepResult("mkvprune", "prune", OK, decision.summary())

    def _ensure_free_space(self, local_path: str) -> None:
        """Refuse to start a prune that can't fit its temp mux. mkvprune writes a ``{stem}.tmp.mkv``
        (~source size) alongside the source before the atomic replace, so peak occupancy is ~2x;
        the *additional* free space required is ~1x the source. Raising here defers the job instead
        of spawning a subprocess that would fail — and instead of dead-lettering on a condition
        that clears itself once disk frees up."""
        try:
            src_size = os.path.getsize(local_path)
            free = shutil.disk_usage(os.path.dirname(local_path) or ".").free
        except OSError as exc:
            log.warning("free-space check skipped for %s: %s", local_path, exc)
            return
        needed = int(src_size * self.services.config.prune_min_free_multiplier)
        if free < needed:
            from orchestrator.util import human_bytes

            raise InsufficientSpaceError(
                f"need ~{human_bytes(needed)} free to prune, have {human_bytes(free)}"
            )

    def _rescan(self, event: ArrEvent) -> None:
        if event.item_id is None:
            return
        client = self.services.arr_client(event.app)
        if client is None:
            return
        try:
            client.rescan_and_wait(event.item_id)
        except Exception as exc:  # non-fatal
            log.warning("%s rescan failed: %s", event.app, exc)

    def _status_rollup(self, event: ArrEvent, outcome: DispatchOutcome) -> None:
        gran = self.services.config.ntfy.status_granularity
        if gran == "action":
            return  # each step was already pinged individually
        any_fail = any(s.result == FAIL for s in outcome.steps)
        did_something = any(s.result == OK for s in outcome.steps)
        if gran == "off" and not any_fail:
            return  # status pings suppressed; failures still alert
        if not any_fail and not did_something:
            return  # everything skipped; stay quiet
        lines = [
            f"- {s.tool}: {s.result}" + (f" — {s.detail or s.error or ''}").rstrip(" —")
            for s in outcome.steps
        ]
        self.services.notifier.send(
            NtfyMessage(
                title=event.label,
                body="\n".join(lines),
                tags=[event.app, "rotating_light" if any_fail else "white_check_mark"],
                priority=PRIORITY_FAILURE if any_fail else PRIORITY_STATUS,
            )
        )

    def _matching_show(self, title: str) -> ShowConfig | None:
        for show in self.services.config.shows:
            if show.name.lower() == title.lower():
                return show
        return None

    def _sonarr_prefetch(self, event: ArrEvent, op: Any) -> dict[str, Any] | None:
        """Look up the Sonarr episode id + its current quality/release group, by the ORIGINAL
        (pre-shift) season/episode — that is how Sonarr tracks the episode."""
        client = self.services.arr_client("sonarr")
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

    def _sonarr_import(self, event: ArrEvent, op: Any, prefetch: dict[str, Any] | None) -> None:
        """Re-register the shifted file with Sonarr under its ORIGINAL episode numbering,
        preserving the prefetched quality/release group (best-effort). Mirrors the standalone
        seasonal-shift's refresh -> manual-import flow."""
        client = self.services.arr_client("sonarr")
        instance = self.services.arr_instance("sonarr")
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


def _scan_single(file: Path):
    m = EPISODE_PATTERN.match(file.name)
    if m:
        yield file, m
