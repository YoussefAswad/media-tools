"""Probe the file and ntfy anything the policy flags."""

from __future__ import annotations

import logging

from mediacore.audit import FAIL, OK
from mediacore.notify import PRIORITY_FINDING
from mediacore.policy.rules import check

from orchestrator.steps.base import JobContext, StepResult

log = logging.getLogger(__name__)


class QualityStep:
    """Findings only — this step never changes a file. Its probe is cached on the context, so
    the prune that follows reuses it instead of paying for a second mkvmerge + ffprobe pass.

    A probe failure is reported as FAIL but does not stop the pipeline: prune will try the probe
    again and fail the job itself if the file really is unreadable.
    """

    name = "quality-check"
    silent = False

    def applies(self, ctx: JobContext) -> bool:
        return True  # being in pipeline.steps is the switch

    def run(self, ctx: JobContext) -> StepResult:
        try:
            pr = ctx.probed()
        except Exception as exc:
            log.warning("probe failed for %s: %s", ctx.local_path, exc)
            return StepResult("quality-check", "probe", FAIL, error=str(exc))

        findings = check(pr, ctx.services.config.policy)
        if not findings:
            return StepResult("quality-check", "check", OK, "no findings")

        # When prune is in this pipeline it is about to fix what was just flagged, and the job
        # rollup restates the findings anyway — measured on a real day, 153 of 153 rollups
        # repeated the finding text a median 8 s later, at a *lower* priority than the finding.
        # Ask ctx.steps (not a config flag) whether prune is configured at all, the same way the
        # Jellyfin refresh tells "hasn't run yet" from "isn't in the pipeline".
        notif = ctx.services.config.notifications
        if not (notif.suppress_findings_when_pruning and "prune" in ctx.steps):
            body = "\n".join(
                f"**Rule {f.rule}** — {f.title}\n" + "\n".join(f"- {d}" for d in f.details)
                for f in findings
            )
            ctx.services.notify.send(
                "quality-findings",
                title=f"{ctx.event.label}",
                body=body,
                tags=[ctx.event.app, "warning"],
                priority=PRIORITY_FINDING,
                topic=ctx.services.config.ntfy.topic,
                job_id=ctx.job_id,
                path=ctx.local_path,
            )
        # Human-readable finding titles (not cryptic rule letters).
        titles = "; ".join(f.title for f in findings)
        return StepResult("quality-check", "check", OK, f"findings: {titles}")
