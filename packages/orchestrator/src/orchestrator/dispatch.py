"""What the worker runs for one claimed job.

Two shapes of job, and the dispatcher routes between them by ``kind``:

* the **per-file pipeline** (an arr webhook, or a re-run synthesized from a path) — an ordered
  registry of steps from :mod:`orchestrator.steps`, run by :class:`Pipeline`;
* a **library scan**, which has no event and no file to change, and so is its own handler.

:class:`Pipeline` owns everything that is the same for every step — timing, the audit record,
the per-action ntfy ping, following a path a step moved, and the closing status rollup — so a
new step is one class in ``steps/`` plus one line in ``steps.default_pipeline``, not an edit
here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from mediacore.arr.events import parse_event
from mediacore.audit import FAIL, OK, SKIP, ActionRecord
from mediacore.notify import PRIORITY_FAILURE, PRIORITY_STATUS
from mediacore.paths import to_local
from mediacore.probe import probe as _default_probe

from orchestrator.runner import run_mkvprune
from orchestrator.services import Services
from orchestrator.steps import (
    DispatchError,
    InsufficientSpaceError,
    JobContext,
    ProbeFn,
    PruneRunner,
    Step,
    StepResult,
    build_pipeline,
)

log = logging.getLogger(__name__)

#: job kinds the dispatcher routes on. The arr kinds both run the per-file pipeline; a library
#: scan is a different shape entirely (no event, no file to change).
ARR_EVENT = "arr_event"
RERUN = "rerun"
LIBRARY_SCAN = "library_scan"

__all__ = [
    "ARR_EVENT",
    "LIBRARY_SCAN",
    "RERUN",
    "DispatchError",
    "DispatchOutcome",
    "Dispatcher",
    "InsufficientSpaceError",
    "Pipeline",
    "StepResult",
]


@dataclass
class DispatchOutcome:
    label: str
    steps: list[StepResult] = field(default_factory=list)
    final_local_path: str | None = None


class Pipeline:
    """Runs an ordered list of steps over one file."""

    def __init__(self, services: Services, steps: list[Step]) -> None:
        self.services = services
        self.steps = steps

    def run(self, ctx: JobContext, *, set_step=None) -> DispatchOutcome:
        outcome = DispatchOutcome(label=ctx.event.label)
        for step in self.steps:
            if not step.applies(ctx):
                continue
            # Silent steps stay out of the job row as well as the rollup: the ledger snapshot
            # and the Jellyfin refresh are over in milliseconds, and flashing them as the
            # current step would only make the row flicker.
            if set_step and not step.silent:
                set_step(step.name)
            t0 = time.monotonic()
            result = step.run(ctx)
            result.duration_ms = int((time.monotonic() - t0) * 1000)

            ctx.results.append(result)
            if not step.silent:
                outcome.steps.append(result)
            self._record(ctx, result)
            if result.new_path:
                ctx.local_path = result.new_path

        outcome.final_local_path = ctx.local_path
        self._status_rollup(ctx, outcome)
        return outcome

    # --- bookkeeping the steps don't do themselves -----------------------------------
    def _record(self, ctx: JobContext, step: StepResult) -> None:
        import os

        path = ctx.local_path
        mtime = os.path.getmtime(path) if path and os.path.exists(path) else None
        self.services.audit.record(
            ActionRecord(
                tool=step.tool,
                action=step.action,
                result=step.result,
                app=ctx.event.app,
                event_type=ctx.event.event_type,
                path=path,
                mtime=mtime,
                after={"detail": step.detail} if step.detail else None,
                error=step.error,
                job_id=ctx.job_id,
                duration_ms=step.duration_ms,
            )
        )
        if step.result != SKIP:
            self._notify_step(ctx, step)

    def _notify_step(self, ctx: JobContext, step: StepResult) -> None:
        event = ctx.event
        pri = PRIORITY_FAILURE if step.result == FAIL else PRIORITY_STATUS
        tag = "rotating_light" if step.result == FAIL else "gear"
        self.services.notify.send(
            "job-step",
            title=f"{event.label} - {step.tool}",
            body=f"**{step.action}**: {step.result}\n\n{step.detail or step.error or ''}",
            tags=[event.app, tag],
            priority=pri,
            job_id=ctx.job_id,
            path=ctx.local_path,
        )

    def _status_rollup(self, ctx: JobContext, outcome: DispatchOutcome) -> None:
        event = ctx.event
        # The per-step pings, when switched on, already said all of this one step at a time.
        if self.services.config.notifications.rule("job-step").enabled:
            return
        any_fail = any(s.result == FAIL for s in outcome.steps)
        did_something = any(s.result == OK for s in outcome.steps)
        if not any_fail and not did_something:
            return  # everything skipped; stay quiet
        lines = [
            f"- {s.tool}: {s.result}" + (f" - {s.detail or s.error or ''}").rstrip(" -")
            for s in outcome.steps
        ]
        self.services.notify.send(
            "job-success",
            title=event.label,
            body="\n".join(lines),
            tags=[event.app, "rotating_light" if any_fail else "white_check_mark"],
            priority=PRIORITY_FAILURE if any_fail else PRIORITY_STATUS,
            job_id=ctx.job_id,
            path=ctx.local_path,
        )


class Dispatcher:
    def __init__(
        self,
        services: Services,
        *,
        probe_fn: ProbeFn | None = None,
        prune_runner: PruneRunner | None = None,
        steps: list[Step] | None = None,
    ) -> None:
        self.services = services
        self.probe_fn: ProbeFn = probe_fn or _default_probe
        self.prune_runner: PruneRunner = prune_runner or run_mkvprune
        # The pipeline is config: `pipeline.steps` names them, in order. An explicit list still
        # wins, which is what lets a test run one step on its own.
        self.pipeline = Pipeline(
            services,
            steps if steps is not None else build_pipeline(services.config.pipeline.steps),
        )

    def dispatch(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        kind: str = ARR_EVENT,
        set_step=None,
        set_progress=None,
    ) -> DispatchOutcome:
        if kind == LIBRARY_SCAN:
            return self._library_scan(
                job_id, payload, set_step=set_step, set_progress=set_progress
            )
        return self._per_file(
            job_id, payload, set_step=set_step, set_progress=set_progress
        )

    # --- the per-file pipeline -------------------------------------------------------
    def _per_file(
        self, job_id: str, payload: dict[str, Any], *, set_step=None, set_progress=None
    ) -> DispatchOutcome:
        event = parse_event(payload["app"], payload["payload"])
        remote_path = event.primary_path()
        if remote_path is None:
            raise DispatchError("event has no file path")
        instance = self.services.arr_instance(event.app)
        local_path = str(to_local(instance, remote_path)) if instance else remote_path

        ctx = JobContext(
            services=self.services,
            job_id=job_id,
            event=event,
            local_path=local_path,
            probe_fn=self.probe_fn,
            prune_runner=self.prune_runner,
            set_progress=set_progress,
            steps=tuple(s.name for s in self.pipeline.steps),
        )
        return self.pipeline.run(ctx, set_step=set_step)

    # --- the library scan ------------------------------------------------------------
    def _library_scan(
        self, job_id: str, payload: dict[str, Any], *, set_step=None, set_progress=None
    ) -> DispatchOutcome:
        """Refresh the Library tab's index of files already on disk.

        Read-only by construction: it probes and calls ``decide()``, and never touches a media
        file. Enqueueing the work it finds is a separate, explicit act from the Library tab.
        """
        from orchestrator import library as lib

        root = payload.get("root") or None
        full = bool(payload.get("full"))
        label = f"library scan{f' — {root}' if root else ''}"
        if set_step:
            set_step("scan")
        t0 = time.monotonic()
        stats = lib.scan(
            self.services,
            full=full,
            root=root,
            probe_fn=self.probe_fn,
            on_progress=set_progress,
        )
        step = StepResult(
            "library",
            "scan",
            OK,
            stats.summary(),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        self.services.audit.record(
            ActionRecord(
                tool=step.tool,
                action=step.action,
                result=step.result,
                path=root,
                after={"detail": step.detail},
                job_id=job_id,
                duration_ms=step.duration_ms,
            )
        )
        self.services.notify.send(
            "library-scan",
            title=label,
            body=stats.summary(),
            tags=["card_index_dividers"],
            priority=PRIORITY_STATUS,
            job_id=job_id,
        )
        return DispatchOutcome(label=label, steps=[step])
