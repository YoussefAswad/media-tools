"""The Bandwidth tab: the download ledger, quota periods, and household per-device WAN usage.

Two measurements live here on purpose. The ledger records what qBittorrent consumed for a
download; the WAN panels record what every device in the house actually pulled from the internet.
The ledger is a subset, so the tab shows both rather than letting one stand in for the other.
"""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.ledger import MANUAL, QBITTORRENT
from orchestrator.monitor.wan import PAUSE_KEY, paused_until
from orchestrator.services import Services
from orchestrator.util import _DATE_RE, RANGES, human_bytes, parse_bytes, parse_range, range_bounds
from orchestrator.wan_usage import display_name, fold_identities
from orchestrator.web.deps import KINDS, page, partial, require_ui_auth, services_of, use_iec

router = APIRouter(dependencies=[Depends(require_ui_auth)])

def _ledger_bounds(
    services: Services, range: str | None, start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """Resolve time-frame bounds. Explicit start/end win; else 'since the current quota period'
    (the default when one exists, or range=='period'); else the preset range key (30d)."""
    if start or end:
        return range_bounds(range, start, end)
    latest = services.quota_periods.latest_date()
    if range in (None, "", "period") and latest:
        return f"{latest}T00:00:00+00:00", None
    return parse_range(range or "30d")


@router.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request) -> HTMLResponse:
    has_period = services_of(request).quota_periods.latest_date() is not None
    return page(request, "downloads.html",
        {"kinds": KINDS, "ranges": list(RANGES.keys()), "has_period": has_period},
    )


@router.get("/ui/ledger", response_class=HTMLResponse)
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
    services = services_of(request)
    lo, hi = _ledger_bounds(services, range, start, end)
    src = source if source in (QBITTORRENT, MANUAL) else None
    rows = services.ledger.list(start=lo, end=hi, kind=type or None, source=src, q=q)
    cfg = services.config.quotas
    return partial(
        request,
        "_ledger.html",
        {
            "rows": rows,
            "kinds": KINDS,
            "iec": use_iec(units),
            # Attribution is edited per row, not through the byte-editing form beside it — see
            # Ledger.set_attribution for why the two must not share the `edited` flag.
            "devices": _device_options(services),
            "quotas": [
                {"key": k, "label": cfg.label_of(k)} for k in sorted(cfg.resolved_quotas())
            ],
            "server_device": cfg.server_device,
        },
    )


def _device_options(services: Services) -> list[str]:
    """Device identities a download could plausibly have come from.

    Everything the router has seen, plus the two named in config — so the list is right on a
    fresh install (nothing seen yet) and on a household whose gaming PC has been off all period.
    """
    cfg = services.config.quotas
    seen = [d.host for d in fold_identities(
        services.wan.by_device(), services.config.monitoring.router.device_aliases
    )]
    named = [n for n in (cfg.server_device, cfg.games_device) if n]
    return sorted(dict.fromkeys(seen + named + list(cfg.devices)))


@router.get("/ui/ledger-summary", response_class=HTMLResponse)
def ui_ledger_summary(
    request: Request, range: str | None = None, start: str | None = None, end: str | None = None,
    units: str | None = None,
) -> HTMLResponse:
    lo, hi = _ledger_bounds(services_of(request), range, start, end)
    t = services_of(request).ledger.totals(start=lo, end=hi)
    iec = use_iec(units)
    tiles = [
        ("Downloaded", human_bytes(t["downloaded"], iec=iec), "queued"),
        ("Uploaded", human_bytes(t["uploaded"], iec=iec), "skip"),
        ("Total bandwidth", human_bytes(t["total"], iec=iec), "ok"),
        ("Entries", str(t["count"]), ""),
    ]
    return partial(request, "_summary.html", {"tiles": tiles})


@router.get("/ui/ledger-chart", response_class=HTMLResponse)
def ui_ledger_chart(
    request: Request, range: str | None = None, start: str | None = None, end: str | None = None,
    units: str | None = None,
) -> HTMLResponse:
    lo, hi = _ledger_bounds(services_of(request), range, start, end)
    series = services_of(request).ledger.daily_series(start=lo, end=hi)
    iec = use_iec(units)
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
    return partial(
        request, "_ledger_chart.html", {"bars": bars, "peak": human_bytes(peak, iec=iec)}
    )


@router.get("/ui/ledger-remaining", response_class=HTMLResponse)
def ui_ledger_remaining(request: Request, units: str | None = None) -> HTMLResponse:
    """Remaining bandwidth for the current quota period. Independent of the selected range;
    only rendered when the current period carries a limit."""
    services = services_of(request)
    c = services.quota_periods.latest()
    if c is None or c.limit_bytes <= 0:
        return HTMLResponse("")  # no card
    used = services.ledger.totals(start=f"{c.start_date}T00:00:00+00:00", end=None)["total"]
    remaining = c.limit_bytes - used
    pct = (used / c.limit_bytes * 100) if c.limit_bytes else 0
    tone = "over" if remaining < 0 else ("warn" if pct >= 85 else "ok")
    iec = use_iec(units)
    ctx = {
        "limit": human_bytes(c.limit_bytes, iec=iec),
        "used": human_bytes(used, iec=iec),
        "remaining": human_bytes(abs(remaining), iec=iec),
        "over": remaining < 0,
        "pct": min(pct, 100),
        "pct_label": round(pct),
        "tone": tone,
        "start_date": c.start_date,
    }
    return partial(request, "_remaining.html", ctx)


@router.get("/ui/quota-periods", response_class=HTMLResponse)
def ui_quota_periods(request: Request) -> HTMLResponse:
    rows = services_of(request).quota_periods.list()
    return partial(request, "_quota_periods.html", {"rows": rows})


@router.post("/quota-periods/add")
def quota_periods_add(
    request: Request,
    start_date: str = Form(...),
    limit: str = Form("0"),
    note: str = Form(""),
) -> Response:
    if _DATE_RE.match(start_date):
        services_of(request).quota_periods.add(
            start_date=start_date, limit_bytes=parse_bytes(limit), note=(note.strip() or None)
        )
    return RedirectResponse("/downloads", status_code=303)


@router.post("/quota-periods/{period_id}/edit")
def quota_periods_edit(
    request: Request,
    period_id: int,
    start_date: str = Form(...),
    limit: str = Form("0"),
    note: str = Form(""),
) -> Response:
    if _DATE_RE.match(start_date):
        services_of(request).quota_periods.update(
            period_id, start_date=start_date, limit_bytes=parse_bytes(limit),
            note=(note.strip() or None),
        )
    return RedirectResponse("/downloads", status_code=303)


@router.post("/quota-periods/{period_id}/delete")
def quota_periods_delete(request: Request, period_id: int) -> Response:
    services_of(request).quota_periods.delete(period_id)
    return RedirectResponse("/downloads", status_code=303)


@router.post("/downloads/add")
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


@router.post("/downloads/{row_id}/edit")
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


@router.post("/downloads/{row_id}/delete")
def downloads_delete(request: Request, row_id: int) -> Response:
    services_of(request).ledger.delete(row_id)
    return RedirectResponse("/downloads", status_code=303)


# --- WAN usage (per-device, from the router) ------------------------------------------------


def _wan_bounds(
    services: Services, range: str | None, start: str | None, end: str | None
) -> tuple[str | None, str | None]:
    """The same window the ledger panels use, so the two sit on one axis and compare honestly."""
    return _ledger_bounds(services, range, start, end)


@router.get("/ui/wan-summary", response_class=HTMLResponse)
def ui_wan_summary(
    request: Request, range: str | None = None, start: str | None = None,
    end: str | None = None, units: str | None = None,
) -> HTMLResponse:
    """Household WAN totals for the window, with the ledger's share of them.

    The ledger counts torrent traffic on one host; this counts every device's internet use. The
    gap between them is the point of the panel, so both are shown rather than one replacing the
    other.
    """
    services = services_of(request)
    if not services.source_enabled("wan"):
        return HTMLResponse("")
    lo, hi = _wan_bounds(services, range, start, end)
    wan = services.wan.totals(start=lo, end=hi)
    iec = use_iec(units)
    # Counted the same way the table below counts its rows, from the same fold. A count of MACs
    # would be a second, contradicting answer to the question the table is already answering.
    devices = fold_identities(
        services.wan.by_device(start=lo, end=hi),
        services.config.monitoring.router.device_aliases,
    )
    tiles: list[dict] = [
        {"value": human_bytes(wan["total"], iec=iec), "label": "Household WAN", "tone": "ok"},
        {"value": human_bytes(wan["downloaded"], iec=iec), "label": "Downloaded", "tone": "queued"},
        {"value": human_bytes(wan["uploaded"], iec=iec), "label": "Uploaded", "tone": "skip"},
        {
            "value": str(len(devices)),
            "label": "Devices seen",
            "tone": "",
            "title": (
                "Distinct devices. Several MAC addresses reporting the same name are one device — "
                "phones and the TV box randomise theirs."
            ),
        },
    ]
    # How much of the household's usage the ledger explains. Only computed over days the WAN
    # record *fully* covers: it begins partway through whatever day the router source was first
    # switched on, while the ledger already holds that whole day, so including it made the share
    # read a confident 100% off a record twenty minutes old. The first complete day is the day
    # after recording began; until one exists there is nothing honest to divide.
    first = services.wan.first_day()
    full_from = _day_after(first) if first else None
    share_lo = max(lo or "", f"{full_from}T00:00:00+00:00") if full_from else None
    covered = services.wan.totals(start=share_lo, end=hi) if share_lo else None
    if covered and covered["total"]:
        ledger_total = services.ledger.totals(start=share_lo, end=hi)["total"]
        share = round(min(ledger_total / covered["total"], 1.0) * 100)
        tiles.append(
            {
                "value": f"{share}%",
                "label": "Tracked as downloads",
                "tone": "",
                "title": (
                    f"Of {human_bytes(covered['total'], iec=iec)} used since {full_from}, "
                    f"{human_bytes(ledger_total, iec=iec)} is accounted for by the ledger below. "
                    "The rest is streaming, updates, browsing and other devices. "
                    f"Recording began partway through {first}, so that day is excluded."
                ),
            }
        )
    elif first:
        tiles.append(
            {
                "value": first,
                "label": "Recording since",
                "small": True,
                "tone": "",
                "title": (
                    "A download share needs at least one full day of WAN data to compare "
                    "against; the first partial day would overstate it."
                ),
            }
        )
    return partial(request, "_summary.html", {"tiles": tiles, "iec": iec})


@router.get("/ui/wan-devices", response_class=HTMLResponse)
def ui_wan_devices(
    request: Request, range: str | None = None, start: str | None = None,
    end: str | None = None, units: str | None = None,
) -> HTMLResponse:
    services = services_of(request)
    lo, hi = _wan_bounds(services, range, start, end)
    aliases = services.config.monitoring.router.device_aliases
    rows = fold_identities(services.wan.by_device(start=lo, end=hi), aliases)
    total = sum(r.total for r in rows)
    # Keyed by identity, not by MAC: a phone that has rotated its address since the window began
    # is one row here but a different MAC in the live snapshot, and joining on MAC showed it "—"
    # while it was actively downloading. Summed, because a group can be live under several at once.
    live: dict[str, int] = {}
    if _wan_live(services):
        for d in services.snapshot("wan").data.devices:
            name = display_name(aliases, d.mac, d.host)
            live[name] = live.get(name, 0) + d.curr_rx + d.curr_tx
    return partial(
        request,
        "_wan_devices.html",
        {
            "rows": [
                {
                    "host": r.host,
                    "macs": r.macs,
                    "kind": r.kind,
                    "rx": r.rx,
                    "tx": r.tx,
                    "total": r.total,
                    "share": (r.total / total * 100) if total else 0,
                    "rate": live.get(r.host),
                }
                for r in rows
            ],
            "enabled": services.source_enabled("wan"),
            "iec": use_iec(units),
        },
    )


def _day_after(day: str) -> str:
    """The YYYY-MM-DD after ``day`` — the first day WAN recording covers end to end."""
    return (_dt.date.fromisoformat(day) + _dt.timedelta(days=1)).isoformat()


def _wan_live(services: Services) -> bool:
    snap = services.snapshot("wan")
    return bool(snap.ok and snap.data is not None and not snap.data.paused)


@router.get("/ui/wan-pause", response_class=HTMLResponse)
def ui_wan_pause(request: Request) -> HTMLResponse:
    """The polling pause control. Only rendered when the source is configured at all."""
    services = services_of(request)
    if not services.source_enabled("wan"):
        return HTMLResponse("")
    until = paused_until(services.queue.get_meta)
    snap = services.snapshot("wan")
    return partial(
        request,
        "_wan_pause.html",
        {
            "paused_until": until,
            "minutes_left": (
                max(1, round((until - _dt.datetime.now(_dt.UTC)).total_seconds() / 60))
                if until
                else 0
            ),
            "unreachable": snap.ok is False,
            "default_minutes": 15,
        },
    )


@router.post("/wan/pause")
def wan_pause(request: Request, minutes: int = Form(15)) -> Response:
    """Stop polling the router for a while.

    Stored as a deadline rather than a flag so it lifts itself: the router accepts one admin
    session, so this exists to hand it back while you are in the router's own UI — and a pause
    left on by accident would quietly stop recording the household's usage.
    """
    minutes = max(1, min(int(minutes or 15), 240))
    until = _dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=minutes)
    services_of(request).queue.set_meta(PAUSE_KEY, until.isoformat(timespec="seconds"))
    return RedirectResponse("/downloads", status_code=303)


@router.post("/wan/resume")
def wan_resume(request: Request) -> Response:
    services_of(request).queue.set_meta(PAUSE_KEY, None)
    return RedirectResponse("/downloads", status_code=303)
