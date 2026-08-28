"""Make Jellyfin re-read the file the pipeline just changed."""

from __future__ import annotations

import logging

from mediacore.audit import OK, SKIP

from orchestrator.steps.base import JobContext, StepResult

log = logging.getLogger(__name__)


class JellyfinStep:
    """A library scan only notices files appearing and disappearing, so a file rewritten in
    place keeps its stale track list and size forever. Refresh that one item instead, and fall
    back to a scan only when the path can't be resolved — a brand-new import, or one the
    pipeline renamed, which is the case a scan is genuinely for.

    Best-effort and silent: never fatal, and out of the ntfy rollup.
    """

    name = "jellyfin"
    silent = True

    def applies(self, ctx: JobContext) -> bool:
        if ctx.services.jellyfin is None:
            return False
        # Only once prune has had its turn (ok or skip), so Jellyfin reads the final file — but
        # still when pruning is switched off entirely, since the import itself is news to it.
        return ctx.ran("mkvprune") or not ctx.services.config.enable_prune

    def run(self, ctx: JobContext) -> StepResult:
        jf = ctx.services.jellyfin
        assert jf is not None  # applies() checked
        detail, ok = "not configured / failed", False
        try:
            item_id = jf.refresh_path(ctx.local_path) if ctx.local_path else None
            if item_id:
                ok, detail = True, f"metadata refreshed (item {item_id})"
            else:
                ok = jf.refresh_library()
                detail = "library scan triggered (item not found)" if ok else detail
        except Exception as exc:
            log.warning("jellyfin refresh error: %s", exc)
        return StepResult("jellyfin", "refresh", OK if ok else SKIP, detail)
