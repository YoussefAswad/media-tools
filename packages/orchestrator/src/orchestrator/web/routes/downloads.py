"""The bandwidth ledger: entries, quota cutoffs, totals and the daily chart."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.ledger import MANUAL, QBITTORRENT
from orchestrator.services import Services
from orchestrator.util import _DATE_RE, RANGES, human_bytes, parse_bytes, parse_range, range_bounds
from orchestrator.web.deps import KINDS, page, partial, require_ui_auth, services_of, use_iec

router = APIRouter(dependencies=[Depends(require_ui_auth)])

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


@router.get("/downloads", response_class=HTMLResponse)
def downloads_page(request: Request) -> HTMLResponse:
    has_cutoff = services_of(request).cutoffs.latest_date() is not None
    return page(request, "downloads.html",
        {"kinds": KINDS, "ranges": list(RANGES.keys()), "has_cutoff": has_cutoff},
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
    lo, hi = _ledger_bounds(services_of(request), range, start, end)
    src = source if source in (QBITTORRENT, MANUAL) else None
    rows = services_of(request).ledger.list(
        start=lo, end=hi, kind=type or None, source=src, q=q
    )
    return partial(
        request, "_ledger.html", {"rows": rows, "kinds": KINDS, "iec": use_iec(units)}
    )


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
    iec = use_iec(units)
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
    return partial(request, "_remaining.html", ctx)


@router.get("/ui/cutoffs", response_class=HTMLResponse)
def ui_cutoffs(request: Request) -> HTMLResponse:
    rows = services_of(request).cutoffs.list()
    return partial(request, "_cutoffs.html", {"rows": rows})


@router.post("/cutoffs/add")
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


@router.post("/cutoffs/{cutoff_id}/edit")
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


@router.post("/cutoffs/{cutoff_id}/delete")
def cutoffs_delete(request: Request, cutoff_id: int) -> Response:
    services_of(request).cutoffs.delete(cutoff_id)
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
