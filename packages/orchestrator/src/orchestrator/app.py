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
from orchestrator.queue import DEAD, FAILED, PROCESSING, QUEUED
from orchestrator.services import Services, build_services
from orchestrator.worker import Worker

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "web" / "templates"))
_STATIC = _HERE / "web" / "static"

CONFIG_ENV = "ORCHESTRATOR_CONFIG"


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


@app.get("/ui/active", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_active(request: Request) -> HTMLResponse:
    jobs = services_of(request).queue.list(statuses=(QUEUED, PROCESSING), limit=100)
    return _TEMPLATES.TemplateResponse(request, "_jobs.html", {"jobs": jobs, "kind": "active"})


@app.get("/ui/failed", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_failed(request: Request) -> HTMLResponse:
    jobs = services_of(request).queue.list(statuses=(FAILED, DEAD), limit=100)
    return _TEMPLATES.TemplateResponse(request, "_jobs.html", {"jobs": jobs, "kind": "failed"})


@app.get("/ui/actions", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_actions(request: Request) -> HTMLResponse:
    actions = services_of(request).audit.recent(limit=60)
    return _TEMPLATES.TemplateResponse(request, "_actions.html", {"actions": actions})


@app.get("/jobs/{job_id}", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def job_detail(request: Request, job_id: str) -> HTMLResponse:
    services = services_of(request)
    job = services.queue.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    actions = services.audit.for_job(job_id)
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
