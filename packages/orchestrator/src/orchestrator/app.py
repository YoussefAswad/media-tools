"""The FastAPI application: lifespan, static files, and the routers.

Everything else lives in :mod:`orchestrator.web` — one router per tab in ``web/routes/``, the
shared template environment and auth in ``web/deps.py``, and the domain-object-to-template
translation in ``web/presenters.py``.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from mediacore.config import find_default_config

from orchestrator.services import build_services
from orchestrator.web.deps import STATIC_DIR
from orchestrator.web.routes import ROUTERS
from orchestrator.worker import Worker

log = logging.getLogger(__name__)

CONFIG_ENV = "ORCHESTRATOR_CONFIG"


def _config_path() -> Path:
    env = os.getenv(CONFIG_ENV)
    return Path(env) if env else find_default_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    services = build_services(_config_path())
    worker = Worker(services)
    worker.start()
    app.state.services = services
    app.state.worker = worker
    logging.basicConfig(level=logging.INFO)
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title="media-orchestrator", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
for router in ROUTERS:
    app.include_router(router)

__all__ = ["app", "build_services"]
