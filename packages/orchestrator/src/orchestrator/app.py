"""FastAPI app: ARR webhook ingest + web UI, sharing one Services instance with the worker."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from mediacore.config import find_default_config, parse_config, read_raw, save_text
from pydantic import ValidationError

from orchestrator.collectors import read_disk
from orchestrator.config import OrchestratorConfig
from orchestrator.ledger import MANUAL, QBITTORRENT
from orchestrator.queue import CANCELED, DEAD, FAILED, PROCESSING, QUEUED
from orchestrator.services import Services, build_services
from orchestrator.util import (
    _DATE_RE,
    RANGES,
    human_bytes,
    human_duration,
    humanize_days,
    kind_icon,
    parse_bytes,
    parse_range,
    range_bounds,
)
from orchestrator.worker import Worker

log = logging.getLogger(__name__)

_HERE = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=str(_HERE / "web" / "templates"))


@pass_context
def _bytes_filter(ctx: dict, value: object) -> str:
    """`{{ x|bytes }}` — honours a per-render `iec` context flag (GiB/MiB vs GB/MB)."""
    return human_bytes(value, iec=bool(ctx.get("iec")))  # type: ignore[arg-type]


_TEMPLATES.env.filters["bytes"] = _bytes_filter
_TEMPLATES.env.filters["icon"] = kind_icon
_STATIC = _HERE / "web" / "static"

_KINDS = ["movie", "show", "game", "software", "other"]


def _use_iec(units: str | None) -> bool:
    """The Downloads unit toggle sends ``units=iec`` for the GiB/MiB (IEC) suffix form."""
    return (units or "").lower() in ("iec", "binary", "gib")

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
    a["elapsed"] = human_duration(a.get("duration_ms"))
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
def healthz(request: Request) -> dict[str, object]:
    """Liveness: the process is up. Also reports whether the worker thread is alive."""
    worker = getattr(request.app.state, "worker", None)
    return {"status": "ok", "worker_alive": bool(worker and worker.is_alive())}


@app.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Readiness: the worker loop is healthy AND the db is reachable. Point the container
    healthcheck here so a wedged worker or unreachable db actually fails the check."""
    worker = getattr(request.app.state, "worker", None)
    problems: list[str] = []
    if worker is None or not worker.healthy():
        problems.append("worker unhealthy")
    try:
        services_of(request).db.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - db failure is hard to simulate
        problems.append(f"db: {exc}")
    if problems:
        return JSONResponse({"status": "unready", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ready"})


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


@app.get("/ui/worker", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_worker(request: Request) -> HTMLResponse:
    q = services_of(request).queue
    paused = q.is_paused()
    counts = q.counts()
    active = counts.get(QUEUED, 0) + counts.get(PROCESSING, 0)
    return _TEMPLATES.TemplateResponse(
        request,
        "_worker.html",
        {"paused": paused, "active": active, "safe": paused and active == 0},
    )


@app.post("/worker/pause", dependencies=[Depends(require_ui_auth)])
def worker_pause(request: Request) -> Response:
    services_of(request).queue.pause()
    return RedirectResponse("/", status_code=303)


@app.post("/worker/resume", dependencies=[Depends(require_ui_auth)])
def worker_resume(request: Request) -> Response:
    services_of(request).queue.resume()
    return RedirectResponse("/", status_code=303)


@app.post("/jobs/retry-dead", dependencies=[Depends(require_ui_auth)])
def jobs_retry_dead(request: Request) -> Response:
    services_of(request).queue.retry_all_dead()
    return RedirectResponse("/", status_code=303)


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
def _ledger_bounds(
    services: Services, range: str | None, start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """Resolve time-frame bounds. Explicit start/end win; else 'since the latest cutoff' (the
    default when a cutoff exists, or when range=='cutoff'); else the preset range key (30d)."""
    if start or end:
        return range_bounds(range, start, end)
    latest = services.cutoffs.latest_date()
    if range in (None, "", "cutoff") and latest:
        return f"{latest}T00:00:00+00:00", None
    return parse_range(range or "30d")


@app.get("/downloads", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def downloads_page(request: Request) -> HTMLResponse:
    has_cutoff = services_of(request).cutoffs.latest_date() is not None
    return _TEMPLATES.TemplateResponse(
        request,
        "downloads.html",
        {"kinds": _KINDS, "ranges": list(RANGES.keys()), "has_cutoff": has_cutoff},
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
    units: str | None = None,
) -> HTMLResponse:
    lo, hi = _ledger_bounds(services_of(request), range, start, end)
    src = source if source in (QBITTORRENT, MANUAL) else None
    rows = services_of(request).ledger.list(
        start=lo, end=hi, kind=type or None, source=src, q=q
    )
    return _TEMPLATES.TemplateResponse(
        request, "_ledger.html", {"rows": rows, "kinds": _KINDS, "iec": _use_iec(units)}
    )


@app.get("/ui/ledger-summary", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_ledger_summary(
    request: Request, range: str | None = None, start: str | None = None, end: str | None = None,
    units: str | None = None,
) -> HTMLResponse:
    lo, hi = _ledger_bounds(services_of(request), range, start, end)
    t = services_of(request).ledger.totals(start=lo, end=hi)
    iec = _use_iec(units)
    tiles = [
        ("Downloaded", human_bytes(t["downloaded"], iec=iec), "queued"),
        ("Uploaded", human_bytes(t["uploaded"], iec=iec), "skip"),
        ("Total bandwidth", human_bytes(t["total"], iec=iec), "ok"),
        ("Entries", str(t["count"]), ""),
    ]
    return _TEMPLATES.TemplateResponse(request, "_summary.html", {"tiles": tiles})


@app.get("/ui/ledger-chart", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_ledger_chart(
    request: Request, range: str | None = None, start: str | None = None, end: str | None = None,
    units: str | None = None,
) -> HTMLResponse:
    lo, hi = _ledger_bounds(services_of(request), range, start, end)
    series = services_of(request).ledger.daily_series(start=lo, end=hi)
    iec = _use_iec(units)
    peak = max((d + u for _, d, u in series), default=0)
    bars = [
        {
            "day": day,
            "down": d,
            "up": u,
            "down_pct": (d / peak * 100) if peak else 0,
            "up_pct": (u / peak * 100) if peak else 0,
            "label": f"{day}: ↓{human_bytes(d, iec=iec)} ↑{human_bytes(u, iec=iec)}",
        }
        for day, d, u in series
    ]
    return _TEMPLATES.TemplateResponse(
        request, "_ledger_chart.html", {"bars": bars, "peak": human_bytes(peak, iec=iec)}
    )


@app.get("/ui/ledger-remaining", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_ledger_remaining(request: Request, units: str | None = None) -> HTMLResponse:
    """Remaining bandwidth for the current quota period (since the latest cutoff). Independent of
    the selected range; only rendered when the latest cutoff carries a limit."""
    services = services_of(request)
    c = services.cutoffs.latest()
    if c is None or c.limit_bytes <= 0:
        return HTMLResponse("")  # no card
    used = services.ledger.totals(start=f"{c.cutoff_date}T00:00:00+00:00", end=None)["total"]
    remaining = c.limit_bytes - used
    pct = (used / c.limit_bytes * 100) if c.limit_bytes else 0
    tone = "over" if remaining < 0 else ("warn" if pct >= 85 else "ok")
    iec = _use_iec(units)
    ctx = {
        "limit": human_bytes(c.limit_bytes, iec=iec),
        "used": human_bytes(used, iec=iec),
        "remaining": human_bytes(abs(remaining), iec=iec),
        "over": remaining < 0,
        "pct": min(pct, 100),
        "pct_label": round(pct),
        "tone": tone,
        "cutoff_date": c.cutoff_date,
    }
    return _TEMPLATES.TemplateResponse(request, "_remaining.html", ctx)


# --- cutoffs (period markers + quota limits) --------------------------------------------
@app.get("/ui/cutoffs", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_cutoffs(request: Request) -> HTMLResponse:
    rows = services_of(request).cutoffs.list()
    return _TEMPLATES.TemplateResponse(request, "_cutoffs.html", {"rows": rows})


@app.post("/cutoffs/add", dependencies=[Depends(require_ui_auth)])
def cutoffs_add(
    request: Request,
    cutoff_date: str = Form(...),
    limit: str = Form("0"),
    note: str = Form(""),
) -> Response:
    if _DATE_RE.match(cutoff_date):
        services_of(request).cutoffs.add(
            cutoff_date=cutoff_date, limit_bytes=parse_bytes(limit), note=(note.strip() or None)
        )
    return RedirectResponse("/downloads", status_code=303)


@app.post("/cutoffs/{cutoff_id}/edit", dependencies=[Depends(require_ui_auth)])
def cutoffs_edit(
    request: Request,
    cutoff_id: int,
    cutoff_date: str = Form(...),
    limit: str = Form("0"),
    note: str = Form(""),
) -> Response:
    if _DATE_RE.match(cutoff_date):
        services_of(request).cutoffs.update(
            cutoff_id, cutoff_date=cutoff_date, limit_bytes=parse_bytes(limit),
            note=(note.strip() or None),
        )
    return RedirectResponse("/downloads", status_code=303)


@app.post("/cutoffs/{cutoff_id}/delete", dependencies=[Depends(require_ui_auth)])
def cutoffs_delete(request: Request, cutoff_id: int) -> Response:
    services_of(request).cutoffs.delete(cutoff_id)
    return RedirectResponse("/downloads", status_code=303)


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


# --- monitoring -------------------------------------------------------------------------
@app.get("/monitoring", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def monitoring_page(request: Request) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(request, "monitoring.html", {})


@app.get("/ui/mon-summary", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_mon_summary(request: Request) -> HTMLResponse:
    services = services_of(request)
    counts = services.alerts.counts()
    crit = counts.get("crit", 0)
    warn = counts.get("warn", 0)
    disks = services.config.monitoring.disks
    reports = [r for r in (read_disk(d) for d in disks) if r is not None]
    ok_disks = sum(1 for r in reports if r.severity == "ok")
    jc = services.queue.counts()
    active_jobs = jc.get(QUEUED, 0) + jc.get(PROCESSING, 0)
    tiles = [
        ("Disks OK", f"{ok_disks}/{len(reports)}", "ok" if ok_disks == len(reports) else "fail"),
        ("Critical", str(crit), "fail" if crit else "skip"),
        ("Warnings", str(warn), "warn" if warn else "skip"),
        ("Jobs active", str(active_jobs), "queued" if active_jobs else "skip"),
    ]
    if services.docker is not None:
        containers = services.docker.containers()
        if containers is not None:
            up = sum(1 for c in containers if c.up)
            tiles.append(
                ("Containers up", f"{up}/{len(containers)}",
                 "ok" if up == len(containers) else "fail")
            )
    return _TEMPLATES.TemplateResponse(request, "_summary.html", {"tiles": tiles})


#: tone class for the reusable `.remaining` gauge, per alert severity.
_DISK_TONE = {"ok": "", "warn": "warn", "crit": "over"}


@app.get("/ui/mon-disks", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_mon_disks(request: Request) -> HTMLResponse:
    services = services_of(request)
    cards = []
    for watch in services.config.monitoring.disks:
        rep = read_disk(watch)
        if rep is None:
            cards.append({"label": watch.label, "path": str(watch.path), "missing": True})
            continue
        rate, eta = _disk_growth(services, rep)
        cards.append(
            {
                "label": rep.label,
                "path": rep.path,
                "missing": False,
                "tone": _DISK_TONE.get(rep.severity, ""),
                "pct": min(rep.pct, 100),
                "pct_label": round(rep.pct),
                "free": human_bytes(rep.free),
                "used": human_bytes(rep.used),
                "total": human_bytes(rep.total),
                "rate": rate,
                "eta": eta,
            }
        )
    return _TEMPLATES.TemplateResponse(request, "_mon_disks.html", {"cards": cards})


def _disk_growth(services: Services, rep) -> tuple[str | None, str | None]:
    """Estimate fill rate (bytes/day) and time-to-full from the last 7d of ``disk.used`` samples.
    Returns human strings, or (None, None) when there isn't enough history / it isn't growing."""
    series = services.metrics.series(f"disk.used:{rep.path}", since_days=7)
    if len(series) < 2:
        return None, None
    (t0, v0), (t1, v1) = series[0], series[-1]
    try:
        span_days = (
            datetime.fromisoformat(t1) - datetime.fromisoformat(t0)
        ).total_seconds() / 86400
    except ValueError:
        return None, None
    if span_days <= 0 or v1 <= v0:
        return None, None
    per_day = (v1 - v0) / span_days
    rate = f"+{human_bytes(per_day)}/day"
    eta = humanize_days(rep.free / per_day) if per_day > 0 else None
    return rate, eta


@app.get("/ui/mon-alerts", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_mon_alerts(request: Request) -> HTMLResponse:
    alerts = services_of(request).alerts.active()
    rows = [
        {
            "label": a.label,
            "severity": a.severity,
            "badge": "fail" if a.severity == "crit" else "warn",
            "detail": a.detail or "",
            "since": (a.since or "").replace("T", " ")[:16],
        }
        for a in alerts
    ]
    return _TEMPLATES.TemplateResponse(request, "_mon_alerts.html", {"alerts": rows})


@app.get("/ui/mon-containers", response_class=HTMLResponse, dependencies=[Depends(require_ui_auth)])
def ui_mon_containers(request: Request) -> HTMLResponse:
    services = services_of(request)
    watch = set(services.config.monitoring.docker.watch)
    dots = []
    unreachable = False
    if services.docker is not None:
        containers = services.docker.containers()
        if containers is None:
            unreachable = True
        else:
            for c in containers:
                if not c.up:
                    tone = "fail"
                elif c.health == "unhealthy":
                    tone = "warn"
                else:
                    tone = "ok"
                dots.append(
                    {
                        "name": c.name,
                        "tone": tone,
                        "title": f"{c.name}: {c.status or c.state}",
                        "watched": c.name in watch,
                    }
                )
    return _TEMPLATES.TemplateResponse(
        request,
        "_mon_containers.html",
        {"dots": dots, "unreachable": unreachable, "enabled": services.docker is not None},
    )


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
