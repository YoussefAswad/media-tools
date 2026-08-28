"""The overview dashboard: widgets composed from the other tabs' partials."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from orchestrator.web.deps import page, require_ui_auth

router = APIRouter(dependencies=[Depends(require_ui_auth)])

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    """Overview dashboard: at-a-glance widgets composed from the other tabs' partials."""
    return page(request, "dashboard.html", {})
