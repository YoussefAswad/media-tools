"""Snapshot the download's consumed bandwidth from qBittorrent."""

from __future__ import annotations

import logging

from mediacore.audit import OK, SKIP

from orchestrator.steps.base import JobContext, StepResult

log = logging.getLogger(__name__)


class LedgerStep:
    """Runs FIRST, so consumed bandwidth is recorded even if a later step fails or the torrent
    is deleted before anyone looks. Independent of the file; idempotent (upsert by infohash).

    Silent: it records an audit row for visibility but stays out of the ntfy rollup, which is
    about what happened to the *file*.
    """

    name = "ledger"
    silent = True

    def applies(self, ctx: JobContext) -> bool:
        return (
            ctx.services.config.enable_ledger
            and ctx.services.qbit is not None
            and bool(ctx.event.download_id)
        )

    def run(self, ctx: JobContext) -> StepResult:
        from orchestrator.util import human_bytes

        event = ctx.event
        qbit = ctx.services.qbit
        assert qbit is not None  # applies() checked
        try:
            t = qbit.torrent_by_hash(event.download_id)
        except Exception as exc:
            log.warning("ledger: qbit lookup failed: %s", exc)
            t = None
        if not t:
            return StepResult("ledger", "record", SKIP, "torrent not found in qBittorrent")

        ctx.services.ledger.record_auto(
            infohash=event.download_id,
            name=t["name"] or event.label,
            kind="movie" if event.app == "radarr" else "episode",
            app=event.app,
            downloaded_bytes=t["downloaded"],
            uploaded_bytes=t["uploaded"],
            size_bytes=t["total_size"],
            completion_on=t["completion_on"],
        )
        detail = f"↓{human_bytes(t['downloaded'])} ↑{human_bytes(t['uploaded'])}"
        return StepResult("ledger", "record", OK, detail)
