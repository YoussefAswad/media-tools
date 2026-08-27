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

from mediacore.notify import PRIORITY_FAILURE, PRIORITY_STATUS, NtfyMessage

from orchestrator.collectors import collect_containers, collect_disks
from orchestrator.dispatch import Dispatcher, InsufficientSpaceError
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
        self._last_disk = 0.0  # sample disks on the first idle tick
        self._last_docker = 0.0  # sample containers on the first idle tick

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
                self._process_one(job.id, job.payload, job.label)
            finally:
                self._busy = False
        log.info("worker stopped")

    # --- job handling ---------------------------------------------------------------
    def _process_one(self, job_id: str, payload: dict, label: str | None) -> None:
        try:
            self.dispatcher.dispatch(
                job_id,
                payload,
                set_step=lambda s: self.services.queue.set_step(job_id, s),
                set_progress=lambda pct, note: self.services.queue.set_progress(job_id, pct, note),
            )
            self.services.queue.mark_done(job_id)
        except InsufficientSpaceError as exc:
            # Not enough disk to prune: defer (long backoff, no attempt consumed) so the job waits
            # for space to free up instead of dead-lettering. Alert once.
            backoff = self.services.config.prune_low_space_backoff_s
            self.services.queue.defer(job_id, backoff, note=f"low disk space: {exc}")
            log.warning("job %s deferred for low disk space: %s", job_id, exc)
            self.services.notifier.send(
                NtfyMessage(
                    title=f"{label or job_id} — low disk space",
                    body=f"Prune deferred (retrying in ~{backoff // 60} min):\n\n{exc}",
                    tags=["floppy_disk", "warning"],
                    priority=PRIORITY_FAILURE,
                )
            )
        except Exception as exc:
            status = self.services.queue.mark_failed(job_id, str(exc))
            log.warning("job %s failed (%s): %s", job_id, status, exc)
            if status == DEAD:
                self.services.notifier.send(
                    NtfyMessage(
                        title=f"{label or job_id} — failed",
                        body=f"Job dead-lettered after retries:\n\n```\n{exc}\n```",
                        tags=["rotating_light"],
                        priority=PRIORITY_FAILURE,
                    )
                )

    # --- metric collection ----------------------------------------------------------
    def _maybe_collect(self) -> None:
        """Sample monitored metrics, each source on its own monotonic gate (independent of
        maintenance and of each other). Best-effort — a collection error must not touch the job
        loop or the heartbeat."""
        mon = self.services.config.monitoring
        if not mon.enabled:
            return
        now = time.monotonic()
        if now - self._last_disk >= mon.disk_interval_s:
            self._last_disk = now
            with contextlib.suppress(Exception):
                self._collect_disks()
        if (
            mon.docker.enabled
            and self.services.docker is not None
            and now - self._last_docker >= mon.docker.interval_s
        ):
            self._last_docker = now
            with contextlib.suppress(Exception):
                self._collect_docker()

    def _collect_disks(self) -> None:
        mon = self.services.config.monitoring
        samples, evals = collect_disks(mon.disks, mon.disk_alert)
        self.services.metrics.record_many(samples)
        self.services.alerts.evaluate(evals, self.services.alert_channels())

    def _collect_docker(self) -> None:
        mon = self.services.config.monitoring
        client = self.services.docker
        assert client is not None
        samples, evals, containers = collect_containers(client, mon.docker, mon.container_alert)
        self.services.metrics.record_many(samples)
        self.services.alerts.evaluate(evals, self.services.alert_channels())
        if containers is None:  # socket unreachable — flag it, keep last good snapshot? no: mark stale
            self.services.docker_ok = False
            return
        # Per-container CPU/mem for the UI list (parallel; running containers only). The web routes
        # read this snapshot rather than hitting the socket per request.
        if mon.docker.stats:
            client.enrich_stats(containers)
        self.services.docker_snapshot = containers
        self.services.docker_ok = True

    # --- periodic maintenance -------------------------------------------------------
    def _maybe_maintain(self) -> None:
        cfg = self.services.config
        now = time.monotonic()
        if now - self._last_maintenance < cfg.maintenance_interval_s:
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
            q.prune_old(cfg.retention_days)
            + self.services.audit.prune_old(cfg.retention_days)
            + self.services.metrics.prune_old(cfg.monitoring.retention_days)
        )
        if removed:
            log.info("retention pruned %d old rows", removed)
            self._maybe_vacuum()

        # 2. dead-letter revive: give transient-cause deaths one more spaced-out chance.
        revived = q.revive_dead(
            cooldown_s=cfg.dead_revive_cooldown_s, max_revives=cfg.dead_revive_max
        )
        if revived:
            log.info("auto-revived %d dead job(s)", revived)

        # 3. quota alert: one ntfy per usage threshold crossed this cutoff period.
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
        cfg = self.services.config
        cutoffs = self.services.cutoffs
        c = cutoffs.latest()
        if c is None or c.limit_bytes <= 0 or not cfg.quota_alert_pcts:
            return
        used = self.services.ledger.totals(
            start=f"{c.cutoff_date}T00:00:00+00:00", end=None
        )["total"]
        pct = (used / c.limit_bytes * 100) if c.limit_bytes else 0
        crossed = [t for t in sorted(cfg.quota_alert_pcts) if pct >= t]
        if not crossed:
            return
        level = crossed[-1]
        if level <= c.alerted_pct:
            return  # already alerted at this (or a higher) level this period
        over = used > c.limit_bytes
        self.services.notifier.send(
            NtfyMessage(
                title=f"Bandwidth quota {round(pct)}% used",
                body=(
                    f"Since {c.cutoff_date}: {human_bytes(used)} of {human_bytes(c.limit_bytes)}"
                    f" ({round(pct)}%){' — OVER LIMIT' if over else ''}."
                ),
                tags=["chart_with_upwards_trend", "rotating_light" if over else "warning"],
                priority=PRIORITY_FAILURE if over else PRIORITY_STATUS,
            )
        )
        cutoffs.set_alerted(c.id, level)
