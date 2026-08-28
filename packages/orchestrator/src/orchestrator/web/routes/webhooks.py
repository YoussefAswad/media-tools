"""Radarr/Sonarr webhook ingest: authenticate, normalize, enqueue, return fast."""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from orchestrator.services import Services
from orchestrator.web.deps import services_of

router = APIRouter(tags=["webhooks"])

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


@router.post("/arr/radarr")
async def arr_radarr(request: Request) -> JSONResponse:
    return await _handle_arr("radarr", request)


@router.post("/arr/sonarr")
async def arr_sonarr(request: Request) -> JSONResponse:
    return await _handle_arr("sonarr", request)
