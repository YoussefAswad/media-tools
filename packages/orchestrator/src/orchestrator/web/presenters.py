"""Turning domain objects into the shapes a template renders.

Tone classes, human strings, row dicts. This is the layer that decides an unhealthy container
reads amber and a down one reads red — deliberately separate from the routes, which should only
be fetching and rendering.
"""

from __future__ import annotations

import json
from datetime import datetime

from orchestrator.config import AlertRule
from orchestrator.util import human_bytes, human_duration, humanize_days

#: tone class for the reusable `.remaining` gauge, per alert severity.
DISK_TONE = {"ok": "", "warn": "warn", "crit": "over"}

#: alert-event kind → badge tone (recovery reads as "ok", everything else by severity).
EVENT_TONE = {"recovery": "ok", "deescalate": "warn"}


def decorate(a: dict) -> dict:
    """Add a human 'change' string (what the step actually did) and a short filename."""
    change = ""
    after = a.get("after")
    if after:
        try:
            change = (json.loads(after) or {}).get("detail", "")
        except (ValueError, TypeError):
            change = ""
    a = dict(a)
    a["change"] = change or (a.get("error") or "")
    a["file"] = (a.get("path") or "").rsplit("/", 1)[-1]
    a["elapsed"] = human_duration(a.get("duration_ms"))
    return a


def rule_tone(value: float, rule: AlertRule) -> str:
    """Tile tone (ok/warn/fail) for a higher-is-worse value against a warn/crit rule."""
    if rule.crit is not None and value >= rule.crit:
        return "fail"
    if rule.warn is not None and value >= rule.warn:
        return "warn"
    return "ok"


def container_row(c, watch: set[str]) -> dict:
    """Presentation for one container: state badge tone/label + human CPU/mem cells."""
    if not c.up:
        tone, label = "fail", c.state or "down"
    elif c.health == "unhealthy":
        tone, label = "warn", "unhealthy"
    elif c.health == "starting":
        tone, label = "queued", "starting"
    else:
        tone, label = "ok", (c.health or "running")
    cpu = f"{c.cpu_pct:.1f}%" if c.cpu_pct is not None else "—"
    if c.mem_used is not None:
        mem_pct = (c.mem_used / c.mem_limit * 100) if c.mem_limit else 0
        mem = f"{human_bytes(c.mem_used)}" + (f" · {mem_pct:.0f}%" if c.mem_limit else "")
    else:
        mem = "—"
    return {"name": c.name, "tone": tone, "label": label, "cpu": cpu, "mem": mem,
            "watched": c.name in watch}


def disk_growth(services, rep, *, iec: bool = False) -> tuple[str | None, str | None]:
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
    rate = f"+{human_bytes(per_day, iec=iec)}/day"
    eta = humanize_days(rep.free / per_day) if per_day > 0 else None
    return rate, eta


def app_filter(jobs, app: str | None):
    """Narrow a job list to one arr. Done in Python because the app lives inside the payload
    JSON, not in a column."""
    if app not in ("radarr", "sonarr"):
        return jobs
    return [j for j in jobs if (j.payload or {}).get("app") == app]
