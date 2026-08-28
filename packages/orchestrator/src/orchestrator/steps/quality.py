"""Probe the file and ntfy anything the policy flags."""

from __future__ import annotations

import logging

from mediacore.audit import FAIL, OK
from mediacore.notify import PRIORITY_FINDING, NtfyMessage
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
        return ctx.services.config.enable_quality_check

    def run(self, ctx: JobContext) -> StepResult:
        try:
            pr = ctx.probed()
        except Exception as exc:
            log.warning("probe failed for %s: %s", ctx.local_path, exc)
            return StepResult("quality-check", "probe", FAIL, error=str(exc))

        findings = check(pr, ctx.services.config.policy)
        if not findings:
            return StepResult("quality-check", "check", OK, "no findings")

        body = "\n".join(
            f"**Rule {f.rule}** — {f.title}\n" + "\n".join(f"- {d}" for d in f.details)
            for f in findings
        )
        ctx.services.notifier.send(
            NtfyMessage(
                title=f"{ctx.event.label}",
                body=body,
                tags=[ctx.event.app, "warning"],
                priority=PRIORITY_FINDING,
                topic=ctx.services.config.ntfy.topic,
            )
        )
        # Human-readable finding titles (not cryptic rule letters).
        titles = "; ".join(f.title for f in findings)
        return StepResult("quality-check", "check", OK, f"findings: {titles}")
