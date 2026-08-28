"""The job queue: active/failed lists, the audit trail, and the worker controls."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.queue import CANCELED, DEAD, FAILED, PROCESSING, QUEUED
from orchestrator.web.deps import page, partial, require_ui_auth, services_of
from orchestrator.web.presenters import app_filter, decorate

router = APIRouter(dependencies=[Depends(require_ui_auth)])

@router.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request) -> HTMLResponse:
    """The job queue: active/failed, recent actions, worker controls, search + filters."""
    return page(request, "jobs.html", {})


@router.get("/ui/summary", response_class=HTMLResponse)
def ui_summary(request: Request) -> HTMLResponse:
    services = services_of(request)
    jc = services.queue.counts()
    ac = services.audit.counts()
    tiles = [
        ("Active", (jc.get("queued", 0) + jc.get("processing", 0)), "queued"),
        ("Done", jc.get("done", 0), "ok"),
        ("Dead", (jc.get("dead", 0) + jc.get("failed", 0) + jc.get("canceled", 0)), "fail"),
        ("Pruned/changed", ac.get("ok", 0), "ok"),
        ("Skipped", ac.get("skip", 0), "skip"),
        ("Failed steps", ac.get("fail", 0), "fail"),
    ]
    return partial(request, "_summary.html", {"tiles": tiles})


@router.get("/ui/worker", response_class=HTMLResponse)
def ui_worker(request: Request, readonly: bool = False) -> HTMLResponse:
    """Worker status. ``readonly`` (used by the overview) hides the pause/resume controls, which
    live on the Jobs tab."""
    q = services_of(request).queue
    paused = q.is_paused()
    counts = q.counts()
    active = counts.get(QUEUED, 0) + counts.get(PROCESSING, 0)
    return partial(
        request,
        "_worker.html",
        {"paused": paused, "active": active, "safe": paused and active == 0, "readonly": readonly},
    )


@router.post("/worker/pause")
def worker_pause(request: Request) -> Response:
    services_of(request).queue.pause()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/worker/resume")
def worker_resume(request: Request) -> Response:
    services_of(request).queue.resume()
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/retry-dead")
def jobs_retry_dead(request: Request) -> Response:
    services_of(request).queue.retry_all_dead()
    return RedirectResponse("/jobs", status_code=303)


@router.get("/ui/active", response_class=HTMLResponse)
def ui_active(request: Request, q: str | None = None, app: str | None = None) -> HTMLResponse:
    jobs = services_of(request).queue.list(statuses=(QUEUED, PROCESSING), q=q, limit=100)
    return partial(
        request, "_jobs.html", {"jobs": app_filter(jobs, app), "kind": "active"}
    )


@router.get("/ui/failed", response_class=HTMLResponse)
def ui_failed(request: Request, q: str | None = None, app: str | None = None) -> HTMLResponse:
    jobs = services_of(request).queue.list(statuses=(FAILED, DEAD, CANCELED), q=q, limit=100)
    return partial(
        request, "_jobs.html", {"jobs": app_filter(jobs, app), "kind": "failed"}
    )


@router.get("/ui/actions", response_class=HTMLResponse)
def ui_actions(
    request: Request,
    q: str | None = None,
    result: str | None = None,
    app: str | None = None,
    limit: int = 80,
) -> HTMLResponse:
    results = [result] if result in ("ok", "skip", "fail") else None
    app = app if app in ("radarr", "sonarr") else None
    limit = max(1, min(limit, 200))
    actions = services_of(request).audit.recent(limit=limit, q=q, results=results, app=app)
    return partial(
        request, "_actions.html", {"actions": [decorate(a) for a in actions]}
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str) -> HTMLResponse:
    services = services_of(request)
    job = services.queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    actions = [decorate(a) for a in services.audit.for_job(job_id)]
    return page(request, "job_detail.html", {"job": job, "actions": actions}
    )


@router.post("/jobs/{job_id}/retry")
def job_retry(request: Request, job_id: str) -> Response:
    services_of(request).queue.retry(job_id)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/cancel")
def job_cancel(request: Request, job_id: str) -> Response:
    services_of(request).queue.cancel(job_id)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/{job_id}/delete")
def job_delete(request: Request, job_id: str) -> Response:
    services_of(request).queue.delete(job_id)
    return RedirectResponse("/jobs", status_code=303)


@router.post("/jobs/clear-dead")
def jobs_clear_dead(request: Request) -> Response:
    services_of(request).queue.clear_dead()
    return RedirectResponse("/jobs", status_code=303)
