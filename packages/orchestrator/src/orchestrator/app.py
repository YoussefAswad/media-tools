"""FastAPI app: ARR webhook ingest + web UI, sharing one Services instance with the worker."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mediacore.config import find_default_config, parse_config, read_raw, save_text
from pydantic import ValidationError

from orchestrator.config import OrchestratorConfig
from orchestrator.ledger import MANUAL, QBITTORRENT
from orchestrator.queue import CANCELED, DEAD, FAILED, PROCESSING, QUEUED
from orchestrator.services import Services, build_services
from orchestrator.util import RANGES, human_bytes, parse_bytes, range_bounds
from orchestrator.worker import Worker

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "web" / "templates"))
_TEMPLATES.env.filters["bytes"] = human_bytes
_STATIC = _HERE / "web" / "static"

_KINDS = ["movie", "show", "game", "software", "other"]

CONFIG_ENV = "ORCHESTRATOR_CONFIG"


def _decorate(a: dict) -> dict:
    """Add a human 'change' string (what the step actually did) and a short filename."""
    import json as _json

    change = ""
    after = a.get("after")
    if after:
        try:
            change = (_json.loads(after) or {}).get("detail", "")
        except (ValueError, TypeError):
            change = ""
    a = dict(a)
    a["change"] = change or (a.get("error") or "")
    a["file"] = (a.get("path") or "").rsplit("/", 1)[-1]
    return a


def _config_path() -> Path:
    env = os.getenv(CONFIG_ENV)
    return Path(env) if env else find_default_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = build_services(_config_path())
    worker = Worker(services)
    worker.start()
    app.state.services = services
    app.state.worker = worker
    logging.basicConfig(level=logging.INFO)
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="media-tools orchestrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

_basic = HTTPBasic(auto_error=False)


def services_of(request: Request) -> Services:
    return request.app.state.services


def require_ui_auth(
    request: Request, creds: HTTPBasicCredentials | None = Depends(_basic)
) -> None:
    """Optional app-level basic auth for UI routes (Caddy may also enforce its own)."""
    cfg = services_of(request).config.web
    if not (cfg.basic_auth_user and cfg.basic_auth_password):
        return
    ok = bool(
        creds
        and secrets.compare_digest(creds.username, cfg.basic_auth_user)
        and secrets.compare_digest(creds.password, cfg.basic_auth_password)
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="auth required",
            headers={"WWW-Authenticate": "Basic"},
        )


# --- health + webhooks ------------------------------------------------------------------
@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _check_secret(request: Request, services: Services) -> None:
    secret = services.config.webhook_secret
    if not secret:
        return
    token = request.headers.get("X-Webhook-Token", "")
    if not secrets.compare_digest(token, secret):
        raise HTTPException(status_code=401, detail="bad webhook token")


async def _handle_arr(app_name: str, request: Request) -> JSONResponse:
    services = services_of(request)
    _check_secret(request, services)
    payload = await request.json()
    event_type = payload.get("eventType", "")
    if event_type == "Test":
        return JSONResponse({"status": "ok", "test": True})

    from mediacore.arr.events import parse_event

    event = parse_event(app_name, payload)
    if not event.is_actionable:
        return JSONResponse({"status": "ignored", "eventType": event_type})

    job_id = services.queue.enqueue(
        "arr_event", {"app": app_name, "payload": payload}, label=event.label
    )
    return JSONResponse({"status": "queued", "job_id": job_id})


@app.post("/arr/radarr")
async def arr_radarr(request: Request) -> JSONResponse:
    return await _handle_arr("radarr", request)


@app.post("/arr/sonarr")
async def arr_sonarr(request: Request) -> JSONResponse:
    return await _handle_arr("sonarr", request)


# --- web UI -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def dashboard(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "dashboard.html", {})


@app.get("/ui/summary", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
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
    return _TEMPLATES.TemplateResponse(request, "_summary.html", {"tiles": tiles})


def _app_filter(jobs, app: str | None):
    if app not in ("radarr", "sonarr"):
        return jobs
    return [j for j in jobs if (j.payload or {}).get("app") == app]


@app.get("/ui/active", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_active(request: Request, q: str | None = None, app: str | None = None) -> HTMLResponse:
    jobs = services_of(request).queue.list(statuses=(QUEUED, PROCESSING), q=q, limit=100)
    return _TEMPLATES.TemplateResponse(
        request, "_jobs.html", {"jobs": _app_filter(jobs, app), "kind": "active"}
    )


@app.get("/ui/failed", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_failed(request: Request, q: str | None = None, app: str | None = None) -> HTMLResponse:
    jobs = services_of(request).queue.list(statuses=(FAILED, DEAD, CANCELED), q=q, limit=100)
    return _TEMPLATES.TemplateResponse(
        request, "_jobs.html", {"jobs": _app_filter(jobs, app), "kind": "failed"}
    )


@app.get("/ui/actions", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_actions(
    request: Request,
    q: str | None = None,
    result: str | None = None,
    app: str | None = None,
) -> HTMLResponse:
    results = [result] if result in ("ok", "skip", "fail") else None
    app = app if app in ("radarr", "sonarr") else None
    actions = services_of(request).audit.recent(limit=80, q=q, results=results, app=app)
    return _TEMPLATES.TemplateResponse(
        request, "_actions.html", {"actions": [_decorate(a) for a in actions]}
    )


@app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def job_detail(request: Request, job_id: str) -> HTMLResponse:
    services = services_of(request)
    job = services.queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    actions = [_decorate(a) for a in services.audit.for_job(job_id)]
    return _TEMPLATES.TemplateResponse(
        request, "job_detail.html", {"job": job, "actions": actions}
    )


@app.post("/jobs/{job_id}/retry", dependencies=[Depends(require_ui_auth)])
def job_retry(request: Request, job_id: str) -> Response:
    services_of(request).queue.retry(job_id)
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_ui_auth)])
def job_cancel(request: Request, job_id: str) -> Response:
    services_of(request).queue.cancel(job_id)
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/{job_id}/delete", dependencies=[Depends(require_ui_auth)])
def job_delete(request: Request, job_id: str) -> Response:
    services_of(request).queue.delete(job_id)
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/clear-dead", dependencies=[Depends(require_ui_auth)])
def jobs_clear_dead(request: Request) -> Response:
    services_of(request).queue.clear_dead()
    return RedirectResponse("/", status_code=303)


# --- downloads bandwidth ledger ---------------------------------------------------------
@app.get("/downloads", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def downloads_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "downloads.html", {"kinds": _KINDS, "ranges": list(RANGES.keys())}
    )


@app.get("/ui/ledger", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_ledger(
    request: Request,
    range: str | None = None,
    start: str | None = None,
    end: str | None = None,
    type: str | None = None,
    source: str | None = None,
    q: str | None = None,
) -> HTMLResponse:
    lo, hi = range_bounds(range, start, end)
    src = source if source in (QBITTORRENT, MANUAL) else None
    rows = services_of(request).ledger.list(
        start=lo, end=hi, kind=type or None, source=src, q=q
    )
    return _TEMPLATES.TemplateResponse(request, "_ledger.html", {"rows": rows, "kinds": _KINDS})


@app.get("/ui/ledger-summary", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_ledger_summary(
    request: Request, range: str | None = None, start: str | None = None, end: str | None = None
) -> HTMLResponse:
    lo, hi = range_bounds(range, start, end)
    t = services_of(request).ledger.totals(start=lo, end=hi)
    tiles = [
        ("Downloaded", human_bytes(t["downloaded"]), "queued"),
        ("Uploaded", human_bytes(t["uploaded"]), "skip"),
        ("Total bandwidth", human_bytes(t["total"]), "ok"),
        ("Entries", str(t["count"]), ""),
    ]
    return _TEMPLATES.TemplateResponse(request, "_summary.html", {"tiles": tiles})


@app.get("/ui/ledger-chart", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_ledger_chart(
    request: Request, range: str | None = None, start: str | None = None, end: str | None = None
) -> HTMLResponse:
    lo, hi = range_bounds(range, start, end)
    series = services_of(request).ledger.daily_series(start=lo, end=hi)
    peak = max((d + u for _, d, u in series), default=0)
    bars = [
        {
            "day": day,
            "down": d,
            "up": u,
            "down_pct": (d / peak * 100) if peak else 0,
            "up_pct": (u / peak * 100) if peak else 0,
            "label": f"{day}: ↓{human_bytes(d)} ↑{human_bytes(u)}",
        }
        for day, d, u in series
    ]
    return _TEMPLATES.TemplateResponse(
        request, "_ledger_chart.html", {"bars": bars, "peak": human_bytes(peak)}
    )


@app.post("/downloads/add", dependencies=[Depends(require_ui_auth)])
def downloads_add(
    request: Request,
    name: str = Form(...),
    kind: str = Form("other"),
    downloaded: str = Form("0"),
    uploaded: str = Form("0"),
    size: str = Form("0"),
    completed_at: str = Form(""),
    notes: str = Form(""),
) -> Response:
    services_of(request).ledger.add_manual(
        name=name.strip() or "Untitled",
        kind=kind.strip() or "other",
        downloaded_bytes=parse_bytes(downloaded),
        uploaded_bytes=parse_bytes(uploaded),
        size_bytes=parse_bytes(size),
        completed_at=(completed_at or None),
        notes=(notes.strip() or None),
    )
    return RedirectResponse("/downloads", status_code=303)


@app.post("/downloads/{row_id}/edit", dependencies=[Depends(require_ui_auth)])
def downloads_edit(
    request: Request,
    row_id: int,
    name: str = Form(...),
    kind: str = Form("other"),
    downloaded: str = Form("0"),
    uploaded: str = Form("0"),
    size: str = Form("0"),
    completed_at: str = Form(""),
    notes: str = Form(""),
) -> Response:
    services_of(request).ledger.update(
        row_id,
        name=name.strip() or "Untitled",
        kind=kind.strip() or "other",
        downloaded_bytes=parse_bytes(downloaded),
        uploaded_bytes=parse_bytes(uploaded),
        size_bytes=parse_bytes(size),
        completed_at=(completed_at or None),
        notes=(notes.strip() or None),
    )
    return RedirectResponse("/downloads", status_code=303)


@app.post("/downloads/{row_id}/delete", dependencies=[Depends(require_ui_auth)])
def downloads_delete(request: Request, row_id: int) -> Response:
    services_of(request).ledger.delete(row_id)
    return RedirectResponse("/downloads", status_code=303)


@app.post("/rerun", dependencies=[Depends(require_ui_auth)])
def rerun(request: Request, app_name: str = Form(...), path: str = Form(...)) -> Response:
    """Re-run the pipeline for a specific file (quality-check + prune; no arr rescan id)."""
    services = services_of(request)
    payload = {
        "app": app_name,
        "payload": {"eventType": "Download", "movieFile": {"path": path}}
        if app_name == "radarr"
        else {"eventType": "Download", "episodeFile": {"path": path}, "series": {"title": ""}},
    }
    services.queue.enqueue("rerun", payload, label=f"re-run {Path(path).name}")
    return RedirectResponse("/", status_code=303)


@app.get("/config", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def config_page(request: Request) -> HTMLResponse:
    services = services_of(request)
    raw = services.config_path.read_text() if services.config_path.exists() else ""
    return _TEMPLATES.TemplateResponse(request, "config.html", {"raw": raw, "error": None})


@app.post("/config", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def config_save(request: Request, raw: str = Form(...)) -> HTMLResponse:
    services = services_of(request)
    try:
        parse_config(OrchestratorConfig, raw, suffix=services.config_path.suffix or ".yaml")
    except (ValidationError, ValueError) as exc:
        return _TEMPLATES.TemplateResponse(
            request, "config.html", {"raw": raw, "error": str(exc)}, status_code=400
        )
    save_text(services.config_path, raw)
    services.reload_config()
    return _TEMPLATES.TemplateResponse(
        request, "config.html", {"raw": raw, "error": None, "saved": True}
    )


# re-exported for tests / readability
__all__ = ["app", "build_services", "read_raw"]
