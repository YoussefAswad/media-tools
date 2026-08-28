"""The Library tab: what the pipeline would do to files already on disk, and queueing it."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.dispatch import LIBRARY_SCAN, RERUN
from orchestrator.library import DEFAULT_SORT, queue_state, resolve_roots, sync_queued_flags
from orchestrator.util import human_bytes
from orchestrator.web.deps import page, partial, require_ui_auth, services_of

router = APIRouter(dependencies=[Depends(require_ui_auth)])

def _rerun_payload(app_name: str, path: str) -> dict:
    """A synthetic Download event for a file already on disk — what both the per-row re-run and
    the Library tab's bulk enqueue put on the queue. There is no arr item id, so the pipeline
    runs quality-check + prune and skips the post-prune rescan."""
    return {
        "app": app_name,
        "payload": {"eventType": "Download", "movieFile": {"path": path}}
        if app_name == "radarr"
        else {"eventType": "Download", "episodeFile": {"path": path}, "series": {"title": ""}},
    }


@router.post("/rerun")
def rerun(request: Request, app_name: str = Form(...), path: str = Form(...)) -> Response:
    """Re-run the pipeline for a specific file (quality-check + prune; no arr rescan id)."""
    services = services_of(request)
    services.queue.enqueue(
        RERUN, _rerun_payload(app_name, path), label=f"re-run {Path(path).name}"
    )
    return RedirectResponse("/jobs", status_code=303)


@router.get("/library", response_class=HTMLResponse)
def library_page(request: Request) -> HTMLResponse:
    """The Library tab: what the pipeline would do to files already on disk, and the controls to
    queue any subset of it."""
    services = services_of(request)
    roots = [
        {"path": str(path), "app": app_name} for path, app_name in resolve_roots(services.config)
    ]
    # First run has exactly one useful action, so open the scan form instead of hiding it
    # behind a toggle above an empty table.
    return page(
        request,
        "library.html",
        {
            "roots": roots,
            "enabled": services.config.library.enabled,
            "first_run": services.library.counts()["total"] == 0,
        },
    )


@router.get("/ui/library", response_class=HTMLResponse)
def ui_library(
    request: Request,
    q: str | None = None,
    app: str | None = None,
    status: str | None = None,
    reason: str | None = None,
    root: str | None = None,
    sort: str | None = None,
    page: int = 1,
) -> HTMLResponse:
    services = services_of(request)
    sync_queued_flags(services)
    size = max(10, min(services.config.library.page_size, 500))
    page = max(1, page)
    rows, total = services.library.list(
        q=q,
        app=app,
        status=status,
        reason=reason,
        root=root,
        sort=sort or DEFAULT_SORT,
        limit=size,
        offset=(page - 1) * size,
    )
    pages = max(1, -(-total // size))
    # What "select all matching" would really queue under these filters — the confirmation
    # dialog quotes it, so it has to be the actionable count, not the row count.
    matching = services.library.actionable_totals(
        q=q, app=app, status=status, reason=reason, root=root
    )
    return partial(
        request,
        "_library.html",
        {
            "rows": rows,
            "total": total,
            "page": min(page, pages),
            "pages": pages,
            "first": (page - 1) * size + 1 if total else 0,
            "last": min(page * size, total),
            "max_enqueue": services.config.library.max_enqueue,
            "matching": matching,
            "sort": sort or DEFAULT_SORT,
        },
    )


@router.get("/ui/library-summary", response_class=HTMLResponse)
def ui_library_summary(request: Request, app: str | None = None) -> HTMLResponse:
    services = services_of(request)
    scan = sync_queued_flags(services)
    c = services.library.counts(app=app if app in ("radarr", "sonarr") else None)
    # A negative delta is space reclaimed; a positive one means the >6-channel compatibility path
    # would ADD a downmix track. Show which way it goes rather than an unsigned number — and show
    # nothing rather than a confident "0 B" when no file's delta could actually be estimated.
    delta = c["delta"]
    sign = "\u2212" if delta < 0 else "+"
    unknown = c["delta_unknown"]
    delta_note = (
        f" {unknown} file(s) don't report per-track sizes and are only partly counted."
        if unknown
        else ""
    )
    tiles = [
        {"label": "Indexed", "value": c["total"]},
        {"label": "Need work", "value": c["needs"], "tone": "queued"},
        {
            "label": "Est. change",
            "note": f"{unknown}?" if unknown else None,
            "value": f"{sign}{human_bytes(abs(delta))}" if delta else "—",
            "value_tone": ("saving" if delta < 0 else "growth") if delta else "",
            "tone": "ok",
            "title": "Net of dropped tracks and re-encoded audio at its new bitrate. Re-encoding "
            "to AC-3 buys player compatibility, not space, so this often grows." + delta_note,
        },
        {"label": "Subtitles", "value": c["subs"], "tone": "skip"},
        {"label": "Re-encode", "value": c["audio"], "tone": "warn"},
        {"label": "Audio drop", "value": c["audiodrop"], "tone": "fail", "hide_when_zero": True},
        {"label": "Errors", "value": c["errors"], "tone": "fail", "hide_when_zero": True},
        {"label": "Queued", "value": c["queued"], "tone": "queued", "hide_when_zero": True},
        _scan_tile(scan, c["last_scan"]),
    ]
    # A scan runs for tens of minutes on the single worker and reports real progress into its job
    # row. Surfacing it here means the tab that starts a scan is also the tab that shows it,
    # instead of sending the reader to /jobs to find out how far along it is.
    return partial(
        request,
        "_library_summary.html",
        {"tiles": tiles, "scanning": scan is not None, "scan": scan},
    )


def _scan_tile(scan, last_scan: str | None) -> dict:
    """The last tile: a live percentage while a scan runs, otherwise when one last finished."""
    if scan is not None and scan.progress is not None:
        value = f"{round(scan.progress)}%"
    elif scan is not None:
        value = "scanning…" if scan.status == "processing" else "queued"
    else:
        value = last_scan[:10] if last_scan else "never"
    return {
        "label": "Scan" if scan is not None else "Last scan",
        "value": value,
        "tone": "queued" if scan is not None else "",
        "small": True,
    }


@router.post("/library/scan")
def library_scan(
    request: Request, root: str = Form(""), full: str = Form("")
) -> Response:
    """Queue an index refresh. ``root`` narrows it to one folder; ``full`` re-probes files the
    (path, mtime) cache would otherwise skip."""
    services = services_of(request)
    if services.config.library.enabled and not queue_state(services)[0]:
        scope = root.strip()
        services.queue.enqueue(
            LIBRARY_SCAN,
            {"root": scope or None, "full": bool(full)},
            label=f"library scan{f' — {scope}' if scope else ''}",
        )
    return RedirectResponse("/library", status_code=303)


@router.post("/library/enqueue")
def library_enqueue(
    request: Request,
    paths: list[str] = Form(default=[]),
    select_all: str = Form(""),
    q: str = Form(""),
    app_filter: str = Form(""),
    status: str = Form(""),
    reason: str = Form(""),
    root: str = Form(""),
) -> Response:
    """Queue the pipeline for the selected files (or every file matching the current filters).

    Targets are resolved through the index, never taken from the form as-is: a path that isn't
    an indexed library file is ignored, so this can't be used to point the pipeline at an
    arbitrary location on disk.
    """
    services = services_of(request)
    cap = max(1, services.config.library.max_enqueue)
    if select_all:
        targets = services.library.paths_matching(
            q=q or None,
            app=app_filter or None,
            status=status or None,
            reason=reason or None,
            root=root or None,
            limit=cap,
        )
    else:
        targets = []
        for path in paths[:cap]:
            row = services.library.get(path)
            if row is not None:
                targets.append((row.path, row.app))
    for path, app_name in targets:
        services.queue.enqueue(
            RERUN, _rerun_payload(app_name, path), label=f"re-run {Path(path).name}"
        )
    services.library.mark_enqueued([p for p, _ in targets])
    return RedirectResponse("/jobs", status_code=303)
