"""Single background worker: claim a job, run the dispatch pipeline, handle retry/dead-letter."""

from __future__ import annotations

import logging
import threading

from mediacore.notify import PRIORITY_FAILURE, NtfyMessage

from orchestrator.dispatch import Dispatcher
from orchestrator.queue import DEAD
from orchestrator.services import Services

log = logging.getLogger(__name__)


class Worker(threading.Thread):
    def __init__(
        self,
        services: Services,
        dispatcher: Dispatcher | None = None,
        *,
        poll_interval: float = 1.0,
    ) -> None:
        super().__init__(name="orchestrator-worker", daemon=True)
        self.services = services
        self.dispatcher = dispatcher or Dispatcher(services)
        self.poll_interval = poll_interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        log.info("worker started")
        while not self._stop.is_set():
            job = self.services.queue.claim_next()
            if job is None:
                self._stop.wait(self.poll_interval)
                continue
            self._process_one(job.id, job.payload, job.label)
        log.info("worker stopped")

    def _process_one(self, job_id: str, payload: dict, label: str | None) -> None:
        try:
            self.dispatcher.dispatch(
                job_id,
                payload,
                set_step=lambda s: self.services.queue.set_step(job_id, s),
            )
            self.services.queue.mark_done(job_id)
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
