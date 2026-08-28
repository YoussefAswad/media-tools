"""Reconciling the library index's "queued" flag against the live job queue.

``library_files.enqueued_at`` is a *cache* of "this file has an active job", stored so the table
can filter and count in SQL rather than cross-referencing the queue per row. Cancelling a job,
deleting it, or letting it dead-letter never calls ``mark_clean``, so the flag has to be
re-derived from the queue on render — otherwise the badge, and the "Queued for re-run" tile,
stick forever. Anything that adds a new way for a job to leave the queue needs nothing extra;
that is the point of deriving it.
"""

from __future__ import annotations

from mediacore.arr.events import parse_event
from mediacore.paths import to_local

from orchestrator.queue import PROCESSING, QUEUED, Job


def queue_state(services) -> tuple[Job | None, set[str]]:
    """One pass over the live queue for both things the Library tab needs from it: the in-flight
    scan job if there is one (so the tab can show its progress, and so a second scan isn't
    queued on top), and the local path of every job still queued or processing."""
    from orchestrator.dispatch import LIBRARY_SCAN

    scan: Job | None = None
    paths: set[str] = set()
    for job in services.queue.list(statuses=(QUEUED, PROCESSING), limit=500):
        if job.kind == LIBRARY_SCAN:
            # prefer the one actually running over one still waiting behind it
            if scan is None or job.status == PROCESSING:
                scan = job
            continue
        payload = job.payload or {}
        app_name = payload.get("app")
        if not app_name or "payload" not in payload:
            continue
        try:
            remote = parse_event(app_name, payload["payload"]).primary_path()
        except Exception:  # a payload shape we can't read tells us nothing about any path
            continue
        if not remote:
            continue
        instance = services.arr_instance(app_name)
        paths.add(str(to_local(instance, remote)) if instance else str(remote))
    return scan, paths


def sync_queued_flags(services) -> Job | None:
    """Re-derive the "queued" flags from the live queue. Returns the in-flight scan job (if any),
    since both come from the same pass."""
    scan, paths = queue_state(services)
    services.library.reconcile_enqueued(paths)
    return scan
