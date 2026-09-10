"""The Quota tab: who the household's bandwidth counted for.

The Bandwidth tab answers "how much, and which device". This one answers "whose", which needs
the config's people and quotas on top of the same two stores. All the arithmetic lives in
:mod:`orchestrator.quota` and is reached through :meth:`Services.attribution`, so this module is
only presentation — and so the tab and the worker's alert cannot answer the same question two
different ways.

Every panel here reads stored data, so all of them poll at ``data-interval="0"``. There is no
live column on this tab: the Bandwidth tab's 60s refresh exists for its "Now" reading, and a
panel that polls a snapshot it does not need is the stale-as-live trap in a different costume.
"""

from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from mediacore.config import replace_mapping_block, save_text

from orchestrator.quota import Attribution
from orchestrator.services import Services
from orchestrator.util import human_bytes
from orchestrator.web.deps import page, partial, require_ui_auth, services_of, use_iec

router = APIRouter(dependencies=[Depends(require_ui_auth)])

#: hue slots for the stacked household bar; cycled, so the palette never runs out.
_SWATCHES = 6


def _period_bounds(
    services: Services, which: str | None = None
) -> tuple[str | None, str | None, str | None]:
    """(start ISO, end ISO, label) for the selected period.

    ``current`` (default) is since the latest period marker; ``previous`` is the one before it,
    ending the day the current one began; ``all`` is everything the WAN store holds. With no
    period configured at all, the window is everything — a household that has not set a reset
    date still deserves a working tab.
    """
    periods = services.quota_periods
    latest = periods.latest()
    if which == "all" or latest is None:
        return None, None, "all recorded usage"
    if which == "previous":
        prev = periods.previous()
        start = prev.effective_start if prev else None
        label = f"{_when(prev)} to {_when(latest)}" if prev else f"before {_when(latest)}"
        return start, latest.effective_start, label
    return latest.effective_start, None, f"since {_when(latest)}"


def _subtraction(services: Services, which: str | None) -> dict:
    """The per-MAC amount to take off this window so a split start day lands on one side.

    The current period keeps its start day minus the snapshot; the previous one keeps only the
    snapshot. Both are driven by the *same* period's split — the boundary belongs to the newer
    period, and its predecessor simply ends there.
    """
    latest = services.quota_periods.latest()
    if which == "all" or latest is None:
        return {}
    return services.period_subtraction(latest, side="before" if which == "previous" else "after")


def _selected_period(services: Services, which: str | None):
    """The period the tab is showing, not simply the newest one.

    The summary compares usage against a limit, and the limit belongs to the period being looked
    at: rendering the current period's cap beside the previous period's usage reported August's
    507 GB as 101% of a 500 GB plan it was never held to (its own was 900 GB). "All time" spans
    every period and so is held to none.
    """
    if which == "all":
        return None
    return services.quota_periods.previous() if which == "previous" else services.quota_periods.latest()


def _when(period) -> str:
    """Where a period's accounting begins, for display.

    The *renewal* time is deliberately not shown here: under the boundary-day rule the accounting
    starts the following midnight, and printing "since 10 Sep 16:37" beside numbers that begin on
    the 11th would be the sort of half-truth this tab exists to avoid. The renewal time is shown
    in its own sentence, where it can be explained.
    """
    return period.effective_start[:10]


@router.get("/quota", response_class=HTMLResponse)
def quota_page(request: Request) -> HTMLResponse:
    services = services_of(request)
    cfg = services.config.quotas
    return page(
        request,
        "quota.html",
        {
            "enabled": cfg.enabled,
            "wan_on": services.source_enabled("wan"),
            "has_previous": services.quota_periods.previous() is not None,
            "people": _people_options(services),
        },
    )


def _people_options(services: Services) -> list[dict]:
    """People, labelled by their own name — never by the quota they count to.

    Labelling them by quota collapsed every person pointed at the shared pot into the same
    entry: three people all reading "Household" in a dropdown you pick an owner from. Where the
    quota differs from the person it is shown *alongside* the name, because "this device becomes
    Joury's, and Joury counts to Household" is the thing being decided.
    """
    cfg = services.config.quotas
    options = []
    for key, person in sorted(cfg.people.items()):
        name = person.label or key.replace("-", " ").replace("_", " ").title()
        quota = cfg.quota_of_person(key)
        options.append(
            {
                "key": key,
                "label": name if quota == key else f"{name} \u2192 {cfg.label_of(quota)}",
                "name": name,
            }
        )
    return options


def _attribution(request: Request, period: str | None) -> tuple[Attribution, str, Services]:
    services = services_of(request)
    lo, hi, label = _period_bounds(services, period)
    result = services.attribution(lo, hi, subtract=_subtraction(services, period))
    return result, label or "", services


@router.get("/ui/quota-summary", response_class=HTMLResponse)
def ui_quota_summary(
    request: Request, period: str | None = None, units: str | None = None
) -> HTMLResponse:
    result, label, services = _attribution(request, period)
    iec = use_iec(units)
    selected = _selected_period(services, period)
    limit = selected.limit_bytes if selected else 0
    pct = (result.household / limit * 100) if limit else 0

    tiles: list[dict] = [
        {
            "value": human_bytes(result.household, iec=iec),
            "label": "Household used",
            "tone": "over" if limit and pct >= 100 else ("warn" if limit and pct >= 85 else "ok"),
            "title": f"Everything the router measured {label}.",
        }
    ]
    if limit:
        tiles += [
            {"value": human_bytes(limit, iec=iec), "label": "Plan limit", "tone": ""},
            {
                "value": f"{round(pct)}%",
                "label": "Of plan",
                "tone": "over" if pct >= 100 else ("warn" if pct >= 85 else "ok"),
            },
        ]
    tiles.append(
        {
            "value": str(len(result.unassigned)),
            "label": "Unowned devices",
            "tone": "warn" if result.unassigned else "",
            "hide_when_zero": False,
            "title": (
                "Devices with no owner in config. They count to the shared quota, which is the "
                "right answer for a TV or a guest phone and a gap for anything else."
            ),
        }
    )
    moved = sum(r.moved for r in result.reassignments)
    if result.reassignments:
        tiles.append(
            {
                "value": human_bytes(moved, iec=iec),
                "label": "Reassigned",
                "tone": "queued",
                "title": (
                    f"{len(result.reassignments)} download(s) counted against a quota other than "
                    "the downloading device's own."
                ),
            }
        )
    return partial(request, "_summary.html", {"tiles": tiles, "iec": iec})


@router.get("/ui/quota-buckets", response_class=HTMLResponse)
def ui_quota_buckets(
    request: Request, period: str | None = None, units: str | None = None
) -> HTMLResponse:
    result, label, services = _attribution(request, period)
    cfg = services.config.quotas
    household = result.household
    rows = []
    for i, q in enumerate(result.quotas):
        share = (q.used / household * 100) if household else 0
        rows.append(
            {
                "key": q.key,
                "label": q.label,
                "shared": q.shared,
                "used": q.used,
                "raw": q.raw,
                "moved_in": q.moved_in,
                "moved_out": q.moved_out,
                "allocation": q.allocation,
                # `pct` is None for a quota with no allocation, and the template must not read
                # that as zero: unbounded is not empty. It shows share-of-household instead.
                "pct": q.pct,
                "pct_capped": min(q.pct, 100) if q.pct is not None else None,
                "tone": q.tone,
                "share": share,
                "swatch": i % _SWATCHES,
                # counted here rather than in the template: the template lists *people* now, and
                # summing their devices there would have to skip the moves mixed into the list
                "device_count": len(q.devices),
                "person_count": sum(1 for p in q.people if p.key),
                "people": [
                    {
                        "key": person.key,
                        "label": person.label,
                        "used": person.used,
                        "moved_out": person.moved_out,
                        "unowned": not person.key,
                        "share": (person.used / q.used * 100) if q.used > 0 else 0,
                        "devices": [
                            {"host": d.host, "kind": d.kind, "total": d.total,
                             "macs": len(d.macs)}
                            for d in person.devices
                        ],
                    }
                    for person in q.people
                ],
                "moves": [
                    {
                        "name": r.name,
                        "bytes": r.moved,
                        "into": r.to_quota == q.key,
                        # the *other* quota's label, never its key — a raw key on the page is
                        # a leak of how this is stored, and reads as a different name entirely
                        "other": cfg.label_of(
                            r.to_quota if r.to_quota != q.key else r.from_quota
                        ),
                    }
                    for r in result.reassignments
                    if q.key in (r.to_quota, r.from_quota) and r.moved
                ],
            }
        )
    return partial(
        request,
        "_quota_buckets.html",
        {
            "rows": rows,
            "household": household,
            "period_label": label,
            "iec": use_iec(units),
            "enabled": services.config.quotas.enabled,
            "wan_on": services.source_enabled("wan"),
            "reconciles": result.reconciles,
            "attributed": result.attributed,
            "dropped": result.dropped,
            "before_record": result.before_record,
            "unknown_targets": result.unknown_targets,
            "wan_first_day": services.wan.first_day(),
            # A quota renewed partway through a day cannot be resolved against wan_usage, which
            # stores one row per device per DAY. The ledger is filtered to the minute; the
            # router's counters cannot be split, so the start day sits in both periods. Say so
            # and show the size of the uncertainty rather than implying a precision the store
            # does not have.
            **_boundary(services, period),
        },
    )


def _boundary(services: Services, period: str | None) -> dict:
    """What the tab needs to say about a period that starts partway through a day."""
    current = services.quota_periods.latest()
    if period == "all" or current is None or not current.mid_day:
        return {"split_day": None, "split_day_total": 0}
    day = current.boundary_day
    snapshot = services.quota_periods.split(current.id) if current.split_captured else {}
    return {
        "split_day": day,
        "split_day_time": current.start_time,
        "split_day_total": services.wan.totals(start=day, end=day)["total"],
        "counts_from": current.effective_start[:10],
        "split_exact": bool(current.split_captured),
        # what the snapshot says predates the renewal — the other period's half of that day
        "split_before": sum(rx + tx for rx, tx in snapshot.values()),
    }


@router.get("/ui/quota-reassignments", response_class=HTMLResponse)
def ui_quota_reassignments(
    request: Request, period: str | None = None, units: str | None = None
) -> HTMLResponse:
    result, _, services = _attribution(request, period)
    cfg = services.config.quotas
    return partial(
        request,
        "_quota_moves.html",
        {
            "rows": [
                {
                    "id": r.row_id,
                    "name": r.name,
                    "device": r.device,
                    "day": r.day,
                    "from_label": cfg.label_of(r.from_quota),
                    "to_label": cfg.label_of(r.to_quota),
                    "moved": r.moved,
                    "requested": r.requested,
                    "clamped": r.clamped,
                    "shortfall": r.shortfall,
                }
                for r in result.reassignments
            ],
            "iec": use_iec(units),
        },
    )


@router.get("/ui/quota-unassigned", response_class=HTMLResponse)
def ui_quota_unassigned(
    request: Request, period: str | None = None, units: str | None = None
) -> HTMLResponse:
    result, _, services = _attribution(request, period)
    return partial(
        request,
        "_quota_unassigned.html",
        {
            "rows": [
                {"host": d.host, "kind": d.kind, "total": d.total, "macs": len(d.macs)}
                for d in result.unassigned
            ],
            "people": _people_options(services),
            "default_label": services.config.quotas.label_of(
                services.config.quotas.default_quota
            ),
            "iec": use_iec(units),
        },
    )


@router.post("/quota/renew")
def quota_renew(request: Request, limit: str = Form(""), note: str = Form("")) -> Response:
    """Start a quota period *now*, dividing today exactly.

    This exists rather than a time field because the two have to agree: the snapshot is a reading
    of a running accumulator, so it is only the pre-period traffic at the instant it is taken. By
    creating the period from that same instant, the start time and the snapshot describe the same
    moment by construction — which a typed time set hours after the fact cannot.

    A period entered by hand still works and falls back to giving its whole start day to the
    previous period; this is the path that does not have to.
    """
    from orchestrator.util import parse_bytes

    services = services_of(request)
    now = _dt.datetime.now(_dt.UTC)
    period_id = services.quota_periods.add(
        start_date=now.date().isoformat(),
        start_time=f"{now:%H:%M}",
        limit_bytes=parse_bytes(limit),
        note=(note.strip() or None),
    )
    services.capture_period_split(period_id)
    return RedirectResponse("/quota", status_code=303)


@router.post("/quota/assign")
def quota_assign(
    request: Request, device: str = Form(...), person: str = Form("")
) -> Response:
    """Give a device an owner (or take one away), by editing the config file in place.

    Only the ``quotas.devices`` block is rewritten — see
    :func:`mediacore.config.replace_mapping_block` for why this is a targeted text edit and not a
    model dump. An unknown person key is ignored rather than written, because writing it would
    make the file fail its own validation on the next load and lock the user out of the Config
    tab that would let them fix it.
    """
    services = services_of(request)
    cfg = services.config.quotas
    name = device.strip()
    owner = person.strip()
    if not name or (owner and owner not in cfg.people):
        return RedirectResponse("/quota", status_code=303)

    devices = dict(cfg.devices)
    if owner:
        devices[name] = owner
    else:
        devices.pop(name, None)

    path = services.config_path
    raw = path.read_text() if path.exists() else ""
    save_text(path, replace_mapping_block(raw, "quotas", "devices", devices))
    services.reload_config()
    return RedirectResponse("/quota", status_code=303)


@router.post("/quota/attribute/{row_id}")
def quota_attribute_row(
    request: Request, row_id: int, device: str = Form(""), quota: str = Form("")
) -> Response:
    """Say who downloaded a ledger entry and which quota it counts against.

    Both blank means "derive it" — the server device, and that device's own quota — which is
    what every historical row still says. An unknown quota is dropped rather than stored, so a
    stale form cannot park bytes in a bucket that does not exist.
    """
    services = services_of(request)
    cfg = services.config.quotas
    target = quota.strip()
    if target and target not in cfg.resolved_quotas():
        target = ""
    services.ledger.set_attribution(
        row_id, device=(device.strip() or None), quota=(target or None)
    )
    return RedirectResponse(request.headers.get("referer") or "/downloads", status_code=303)

