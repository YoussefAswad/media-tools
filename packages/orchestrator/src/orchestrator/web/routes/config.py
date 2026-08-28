"""The raw YAML config editor: validate, save atomically, hot-reload."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from mediacore.config import parse_config, save_text
from pydantic import ValidationError

from orchestrator.config import OrchestratorConfig
from orchestrator.web.deps import page, require_ui_auth, services_of

router = APIRouter(dependencies=[Depends(require_ui_auth)])

@router.get("/config", response_class=HTMLResponse)
def config_page(request: Request) -> HTMLResponse:
    services = services_of(request)
    raw = services.config_path.read_text() if services.config_path.exists() else ""
    return page(request, "config.html", {"raw": raw, "error": None})


@router.post("/config", response_class=HTMLResponse)
def config_save(request: Request, raw: str = Form(...)) -> HTMLResponse:
    services = services_of(request)
    try:
        parse_config(OrchestratorConfig, raw, suffix=services.config_path.suffix or ".yaml")
    except (ValidationError, ValueError) as exc:
        return page(request, "config.html", {"raw": raw, "error": str(exc)}, status_code=400
        )
    save_text(services.config_path, raw)
    services.reload_config()
    return page(request, "config.html", {"raw": raw, "error": None, "saved": True}
    )
