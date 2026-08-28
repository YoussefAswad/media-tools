"""Liveness and readiness. No auth: the container healthcheck calls these."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from orchestrator.web.deps import services_of

router = APIRouter(tags=["health"])

@router.get("/healthz")
def healthz(request: Request) -> dict[str, object]:
    """Liveness: the process is up. Also reports whether the worker thread is alive."""
    worker = getattr(request.app.state, "worker", None)
    return {"status": "ok", "worker_alive": bool(worker and worker.is_alive())}


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    """Readiness: the worker loop is healthy AND the db is reachable. Point the container
    healthcheck here so a wedged worker or unreachable db actually fails the check."""
    worker = getattr(request.app.state, "worker", None)
    problems: list[str] = []
    if worker is None or not worker.healthy():
        problems.append("worker unhealthy")
    try:
        services_of(request).db.execute("SELECT 1").fetchone()
    except Exception as exc:  # pragma: no cover - db failure is hard to simulate
        problems.append(f"db: {exc}")
    if problems:
        return JSONResponse({"status": "unready", "problems": problems}, status_code=503)
    return JSONResponse({"status": "ready"})
