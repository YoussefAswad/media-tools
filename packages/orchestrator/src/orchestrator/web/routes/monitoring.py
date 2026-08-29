"""The Monitoring tab: disks, alerts, host system, drive SMART, containers and the router."""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from orchestrator.monitor import read_disk
from orchestrator.monitor.wan import paused_until
from orchestrator.queue import PROCESSING, QUEUED
from orchestrator.util import human_bytes, human_uptime, humanize_days
from orchestrator.web.deps import page, partial, require_ui_auth, services_of, use_iec
from orchestrator.web.presenters import (
    DISK_TONE,
    EVENT_TONE,
    container_row,
    disk_growth,
    rule_tone,
)

router = APIRouter(dependencies=[Depends(require_ui_auth)])

@router.get("/monitoring", response_class=HTMLResponse)
def monitoring_page(request: Request) -> HTMLResponse:
    return page(request, "monitoring.html", {})


@router.get("/ui/mon-summary", response_class=HTMLResponse)
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
    # Read the worker's snapshot, never the socket: this partial polls every 5s, and a live
    # containers() call here meant a Docker round-trip per poll per viewer.
    if services.source_enabled("docker"):
        containers = services.snapshot("docker").rows
        if containers:
            up = sum(1 for c in containers if c.up)
            tiles.append(
                ("Containers up", f"{up}/{len(containers)}",
                 "ok" if up == len(containers) else "fail")
            )
    return partial(request, "_summary.html", {"tiles": tiles})


@router.get("/ui/mon-disks", response_class=HTMLResponse)
def ui_mon_disks(request: Request, units: str | None = None) -> HTMLResponse:
    services = services_of(request)
    iec = use_iec(units)
    cards = []
    for watch in services.config.monitoring.disks:
        rep = read_disk(watch)
        if rep is None:
            cards.append({"label": watch.label, "path": str(watch.path), "missing": True})
            continue
        rate, eta = disk_growth(services, rep, iec=iec)
        cards.append(
            {
                "label": rep.label,
                "path": rep.path,
                "missing": False,
                "tone": DISK_TONE.get(rep.severity, ""),
                "pct": min(rep.pct, 100),
                "pct_label": round(rep.pct),
                "free": human_bytes(rep.free, iec=iec),
                "used": human_bytes(rep.used, iec=iec),
                "total": human_bytes(rep.total, iec=iec),
                "rate": rate,
                "eta": eta,
            }
        )
    return partial(request, "_mon_disks.html", {"cards": cards})


@router.get("/ui/mon-alerts", response_class=HTMLResponse)
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
    return partial(request, "_mon_alerts.html", {"alerts": rows})


@router.get("/ui/mon-events", response_class=HTMLResponse)
def ui_mon_events(request: Request) -> HTMLResponse:
    events = services_of(request).alerts.recent_events(limit=50)
    rows = [
        {
            "when": (e["ts"] or "").replace("T", " ")[:16],
            "label": e["label"],
            "kind": e["kind"],
            "badge": EVENT_TONE.get(e["kind"]) or ("fail" if e["severity"] == "crit" else "warn"),
            "detail": e["detail"] or "",
        }
        for e in events
    ]
    return partial(request, "_mon_events.html", {"events": rows})


@router.get("/ui/mon-system", response_class=HTMLResponse)
def ui_mon_system(request: Request) -> HTMLResponse:
    services = services_of(request)
    mon = services.config.monitoring
    if not services.source_enabled("system"):
        return partial(request, "_mon_system.html", {"enabled": False})
    snap = services.snapshot("system").data
    tiles: list = []
    cpu_model = ""
    if snap is not None:
        cpu_model = snap.cpu_model
        tiles.append(("CPU load", f"{snap.cpu_load:.0f}%", "queued"))
        if snap.cpu_temp is not None:
            tiles.append(
                ("CPU temp", f"{snap.cpu_temp:.0f}°C", rule_tone(snap.cpu_temp, mon.cpu_temp_alert))
            )
        tiles.append(
            (f"RAM · {human_bytes(snap.mem_used)}/{human_bytes(snap.mem_total)}",
             f"{snap.mem_pct:.0f}%", rule_tone(snap.mem_pct, mon.mem_alert))
        )
        tiles.append(("Net down", f"{human_bytes(snap.net_down)}/s", "skip"))
        tiles.append(("Net up", f"{human_bytes(snap.net_up)}/s", "skip"))
        tiles.append(("Uptime", human_uptime(snap.uptime_s) or "—", "skip"))
    return partial(
        request,
        "_mon_system.html",
        {
            "enabled": True,
            "status": services.snapshot("system").ok,
            "tiles": tiles,
            "cpu_model": cpu_model,
        },
    )


@router.get("/ui/mon-smart", response_class=HTMLResponse)
def ui_mon_smart(request: Request) -> HTMLResponse:
    services = services_of(request)
    mon = services.config.monitoring
    smart = services.snapshot("smart")
    rows = []
    for d in smart.rows:
        if not d.passed:
            tone, health = "fail", "FAILED"
        else:
            tone, health = "ok", "passed"
        if d.temp is None:
            temp, temp_tone = "—", ""
        else:
            temp_tone = rule_tone(d.temp, mon.smart_temp_alert)
            temp = f"{d.temp}°C"
        age = humanize_days(d.power_on_hours / 24) if d.power_on_hours else "—"
        rows.append({"name": d.name, "model": d.model, "tone": tone, "health": health,
                     "temp": temp, "temp_tone": temp_tone, "age": age})
    ctx = {"rows": rows, "enabled": services.source_enabled("smart"), "status": smart.ok}
    return partial(request, "_mon_smart.html", ctx)


@router.get("/ui/mon-containers", response_class=HTMLResponse)
def ui_mon_containers(request: Request) -> HTMLResponse:
    services = services_of(request)
    watch = set(services.config.monitoring.docker.watch)
    docker = services.snapshot("docker")
    ctx = {
        "rows": [container_row(c, watch) for c in docker.rows],
        "enabled": services.source_enabled("docker"),
        "status": docker.ok,  # None = collecting, True = ok, False = unreachable
    }
    return partial(request, "_mon_containers.html", ctx)


@router.get(
    "/ui/mon-containers-summary",
    response_class=HTMLResponse,
)
def ui_mon_containers_summary(request: Request) -> HTMLResponse:
    """Compact container widget for the overview: an up/total count plus any down/unhealthy names."""
    services = services_of(request)
    docker = services.snapshot("docker")
    containers = docker.rows
    up = total = 0
    problems: list[dict] = []
    if containers:
        total = len(containers)
        up = sum(1 for c in containers if c.up)
        for c in containers:
            if not c.up:
                problems.append({"name": c.name, "tone": "fail", "label": c.state or "down"})
            elif c.health == "unhealthy":
                problems.append({"name": c.name, "tone": "warn", "label": "unhealthy"})
    ctx = {
        "enabled": services.source_enabled("docker"),
        "status": docker.ok,
        "up": up,
        "total": total,
        "problems": problems,
    }
    return partial(request, "_mon_containers_summary.html", ctx)


@router.get("/ui/mon-wan", response_class=HTMLResponse)
def ui_mon_wan(request: Request, units: str | None = None) -> HTMLResponse:
    """Live per-device WAN rates. Reads the worker's snapshot, never the router — this partial
    polls every 15s and the router only tolerates one session (see pitfall 11's regression)."""
    services = services_of(request)
    snap = services.snapshot("wan")
    data = snap.data
    today = _dt.datetime.now(_dt.UTC).date().isoformat()
    # Read the durable flag, not the snapshot's copy of it: the snapshot only learns about a pause
    # on the next collection, so between pressing Pause and the next poll this panel would keep
    # rendering the last rates as if they were live.
    until = paused_until(services.queue.get_meta) or (data.paused_until if data else None)
    return partial(
        request,
        "_mon_wan.html",
        {
            "enabled": services.source_enabled("wan"),
            "status": snap.ok,
            "rows": sorted(
                data.devices if data else [],
                key=lambda d: d.curr_rx + d.curr_tx,
                reverse=True,
            ),
            "rate_rx": data.rate_rx if data else 0,
            "rate_tx": data.rate_tx if data else 0,
            "today": services.wan.totals(start=today, end=today)["total"],
            "last_poll": (data.last_poll or "")[11:19] if data and data.last_poll else None,
            "paused_until": until,
            "minutes_left": (
                max(1, round((until - _dt.datetime.now(_dt.UTC)).total_seconds() / 60))
                if until
                else 0
            ),
            "iec": use_iec(units),
        },
    )
