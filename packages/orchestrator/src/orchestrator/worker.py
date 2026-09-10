"""Single background worker: claim a job, run the dispatch pipeline, handle retry/dead-letter.

Also runs lightweight periodic maintenance (retention pruning, dead-letter revive, quota alerts)
on this same thread between job claims — no extra threads, so the shared SQLite connection stays
driven serially. A monotonic heartbeat lets the app report real worker liveness on ``/readyz``.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from mediacore.notify import PRIORITY_FAILURE, PRIORITY_STATUS

from orchestrator.dispatch import ARR_EVENT, Dispatcher, InsufficientSpaceError
from orchestrator.monitor import MonitorSource, SourceSnapshot
from orchestrator.queue import DEAD
from orchestrator.services import Services
from orchestrator.util import human_bytes

log = logging.getLogger(__name__)


class Worker(threading.Thread):
    def __init__(
        self,
        services: Services,
        dispatcher: Dispatcher | None = None,
        *,
        poll_interval: float = 1.0,
        healthy_after_s: float = 30.0,
    ) -> None:
        super().__init__(name="orchestrator-worker", daemon=True)
        self.services = services
        self.dispatcher = dispatcher or Dispatcher(services)
        self.poll_interval = poll_interval
        self.healthy_after_s = healthy_after_s
        self._stop = threading.Event()
        self._busy = False
        self.last_beat = time.monotonic()
        self._last_maintenance = 0.0  # run maintenance on the first idle tick
        #: source name -> monotonic time of its last collection. Empty, so every source is
        #: sampled on the first idle tick.
        self._last_collect: dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

    def healthy(self) -> bool:
        """True when the loop is live: the thread is running AND it is either mid-job or has ticked
        recently. A long-running prune keeps ``_busy`` set, so a legitimate multi-minute mux does
        not read as unhealthy (which could otherwise get the container killed mid-prune)."""
        if not self.is_alive():
            return False
        if self._busy:
            return True
        return (time.monotonic() - self.last_beat) <= self.healthy_after_s

    def run(self) -> None:
        log.info("worker started")
        while not self._stop.is_set():
            self.last_beat = time.monotonic()
            try:
                self._maybe_maintain()
                self._maybe_collect()
                job = self.services.queue.claim_next()
            except Exception as exc:  # never let the loop thread die silently
                log.exception("worker loop error: %s", exc)
                self._stop.wait(self.poll_interval)
                continue
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            self._busy = True
            try:
                self._process_one(job.id, job.payload, job.label, job.kind)
            finally:
                self._busy = False
        log.info("worker stopped")

    # --- job handling ---------------------------------------------------------------
    def _process_one(
        self, job_id: str, payload: dict, label: str | None, kind: str = ARR_EVENT
    ) -> None:
        try:
            self.dispatcher.dispatch(
                job_id,
                payload,
                kind=kind,
                set_step=lambda s: self.services.queue.set_step(job_id, s),
                set_progress=lambda pct, note: self.services.queue.set_progress(job_id, pct, note),
            )
            self.services.queue.mark_done(job_id)
        except InsufficientSpaceError as exc:
            # Not enough disk to prune: defer (long backoff, no attempt consumed) so the job waits
            # for space to free up instead of dead-lettering. Alert once.
            backoff = self.services.config.pipeline.low_space_backoff_s
            self.services.queue.defer(job_id, backoff, note=f"low disk space: {exc}")
            log.warning("job %s deferred for low disk space: %s", job_id, exc)
            self.services.notify.send(
                "job-deferred",
                title=f"{label or job_id} - low disk space",
                body=f"Prune deferred (retrying in ~{backoff // 60} min):\n\n{exc}",
                tags=["floppy_disk", "warning"],
                priority=PRIORITY_FAILURE,
                job_id=job_id,
            )
        except Exception as exc:
            status = self.services.queue.mark_failed(job_id, str(exc))
            log.warning("job %s failed (%s): %s", job_id, status, exc)
            if status == DEAD:
                # once_per: job by default — a dead job that the auto-revive picks up and that
                # dies again is the same failure, and used to notify twice (60 messages for 30
                # files in one measured window).
                self.services.notify.send(
                    "job-failed",
                    title=f"{label or job_id} - failed",
                    body=f"Job dead-lettered after retries:\n\n```\n{exc}\n```",
                    tags=["rotating_light"],
                    priority=PRIORITY_FAILURE,
                    job_id=job_id,
                )

    # --- metric collection ----------------------------------------------------------
    def _maybe_collect(self) -> None:
        """Sample every monitored source that is due, each on its own interval gate. Best-effort:
        a collection error must not touch the job loop or the heartbeat."""
        mon = self.services.config.monitoring
        if not mon.enabled:
            return
        now = time.monotonic()
        for source in self.services.sources:
            if not source.enabled:
                continue
            if now - self._last_collect.get(source.name, 0.0) < source.interval_s:
                continue
            self._last_collect[source.name] = now
            with contextlib.suppress(Exception):
                self._collect(source)

    def _collect(self, source: MonitorSource) -> None:
        """Store one source's samples, alert on its evaluations, publish its snapshot.

        ``data is None`` means the underlying service is unreachable: recorded as ``ok=False``
        so the UI says so, rather than leaving the last good numbers on screen looking live.
        """
        samples, evals, data = source.collect()
        self.services.metrics.record_many(samples)
        self.services.alerts.evaluate(evals, self.services.alert_channels())
        self.services.snapshots[source.name] = SourceSnapshot(data=data, ok=data is not None)

    # --- periodic maintenance -------------------------------------------------------
    def _maybe_maintain(self) -> None:
        cfg = self.services.config
        now = time.monotonic()
        if now - self._last_maintenance < cfg.worker.maintenance_interval_s:
            return
        self._last_maintenance = now
        with contextlib.suppress(Exception):
            self._maintain()

    def _maintain(self) -> None:
        cfg = self.services.config
        q = self.services.queue

        # 1. retention: trim finished jobs + old audit rows (JSONL kept as source of truth) and
        #    old metric samples (their own retention window).
        removed = (
            q.prune_old(cfg.worker.retention_days)
            + self.services.audit.prune_old(cfg.worker.retention_days)
            + self.services.metrics.prune_old(cfg.monitoring.retention_days)
            + self.services.alerts.prune_old(cfg.monitoring.retention_days)
            + self.services.notify.prune_old(cfg.worker.retention_days)
        )
        if removed:
            log.info("retention pruned %d old rows", removed)
            self._maybe_vacuum()

        # 2. dead-letter revive: give transient-cause deaths one more spaced-out chance.
        revived = q.revive_dead(
            cooldown_s=cfg.worker.dead_revive_cooldown_s, max_revives=cfg.worker.dead_revive_max
        )
        if revived:
            log.info("auto-revived %d dead job(s)", revived)

        # 3. quota alert: one ntfy per usage threshold crossed this quota period.
        self._check_quota()

    def _maybe_vacuum(self) -> None:
        """VACUUM at most once per day (gated by a meta timestamp) to reclaim pruned space."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if self.services.queue.get_meta("last_vacuum") == today:
            return
        with contextlib.suppress(Exception):
            self.services.db.commit()
            self.services.db.execute("VACUUM")
            self.services.queue.set_meta("last_vacuum", today)

    def _check_quota(self) -> None:
        """Alert on the household's plan, and on any quota that has an allocation.

        **Measured from the router, not from the ledger.** This used to read
        ``ledger.totals()``, which counts what the pipeline downloaded and nothing else — over one
        real period that read 409.05 GB against 505.36 GB actually consumed, so the alert would
        have reported 81% of a 500 GB plan while the household was already over it. The ledger
        is a record of downloads; the link is what a quota is about.

        Thresholds fire once each per period: the household's high-water mark lives on the period
        row, each quota's in ``quota_alerts``. A quota with no allocation raises nothing — it is
        tracked, not bounded, and inventing a threshold for it would page about a pot whose size
        nobody has decided.
        """
        cfg = self.services.config
        periods = self.services.quota_periods
        period = periods.latest()
        pcts = sorted(cfg.worker.quota_alert_pcts)
        if period is None or not pcts:
            return
        # the period's exact start, so a quota renewed midday does not count the whole day
        result = self.services.attribution(period.effective_start, None)

        if period.limit_bytes > 0:
            level = self._crossed(result.household, period.limit_bytes, pcts, period.alerted_pct)
            if level is not None:
                self._send_quota_alert(
                    name="Household",
                    used=result.household,
                    limit=period.limit_bytes,
                    since=_since(period),
                    event="quota",
                )
                periods.set_alerted(period.id, level)

        already = periods.alerted_quotas(period.id)
        for quota in result.quotas:
            if quota.allocation <= 0:
                continue
            level = self._crossed(
                quota.used, quota.allocation, pcts, already.get(quota.key, 0)
            )
            if level is None:
                continue
            self._send_quota_alert(
                name=quota.label,
                used=quota.used,
                limit=quota.allocation,
                since=_since(period),
                event="quota-person",
            )
            periods.set_quota_alerted(period.id, quota.key, level)

    @staticmethod
    def _crossed(used: int, limit: int, pcts: list[int], already: int) -> int | None:
        """The highest threshold this usage crosses, or None if none is newly crossed."""
        if limit <= 0:
            return None
        pct = used / limit * 100
        crossed = [t for t in pcts if pct >= t]
        if not crossed or crossed[-1] <= already:
            return None
        return crossed[-1]

    def _send_quota_alert(
        self, *, name: str, used: int, limit: int, since: str, event: str
    ) -> None:
        pct = round(used / limit * 100) if limit else 0
        over = used > limit
        # ASCII hyphen deliberately: ntfy header text is latin-1, and an em dash arrives as "?".
        self.services.notify.send(
            event,
            title=f"{name} quota {pct}% used",
            body=(
                f"Since {since}: {human_bytes(used)} of {human_bytes(limit)}"
                f" ({pct}%){' - OVER LIMIT' if over else ''}."
            ),
            tags=["chart_with_upwards_trend", "rotating_light" if over else "warning"],
            priority=PRIORITY_FAILURE if over else PRIORITY_STATUS,
        )


def _since(period) -> str:
    """The date an alert's figures are measured from — the effective start, not the renewal
    moment, so the number and the date it quotes come from the same window. ASCII only: ntfy
    header text is latin-1."""
    return period.effective_start[:10]
