"""Shared web plumbing: the template environment, the services accessor, and UI auth.

Every router imports from here, so the Jinja environment (and its filters) is configured once.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context

from orchestrator.services import Services
from orchestrator.util import human_bytes, kind_icon

_HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(_HERE / "templates"))
STATIC_DIR = _HERE / "static"

#: download types offered by the ledger's filters and forms.
KINDS = ["movie", "show", "game", "software", "other"]


@pass_context
def _bytes_filter(ctx: dict, value: object) -> str:
    """`{{ x|bytes }}` — honours a per-render `iec` context flag (GiB/MiB vs GB/MB)."""
    return human_bytes(value, iec=bool(ctx.get("iec")))  # type: ignore[arg-type]


TEMPLATES.env.filters["bytes"] = _bytes_filter
TEMPLATES.env.filters["icon"] = kind_icon

_basic = HTTPBasic(auto_error=False)


def services_of(request: Request) -> Services:
    return request.app.state.services


def partial(
    request: Request, name: str, ctx: dict | None = None, *, status_code: int = 200
) -> HTMLResponse:
    """Render a `_partial.html` for a polling panel. Every `/ui/*` route ends in one of these,
    so the boilerplate is worth exactly one function."""
    return TEMPLATES.TemplateResponse(request, name, ctx or {}, status_code=status_code)


def page(
    request: Request, name: str, ctx: dict | None = None, *, status_code: int = 200
) -> HTMLResponse:
    """Render a full page (one that extends base.html). Same mechanics as :func:`partial`; the
    two names are what tells a reader which kind of response a route returns."""
    return TEMPLATES.TemplateResponse(request, name, ctx or {}, status_code=status_code)


def use_iec(units: str | None) -> bool:
    """The Downloads unit toggle sends ``units=iec`` for the GiB/MiB (IEC) suffix form."""
    return (units or "").lower() in ("iec", "binary", "gib")


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
