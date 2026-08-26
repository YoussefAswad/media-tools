"""Console entrypoint: run the FastAPI app (webhooks + web UI) under uvicorn.

Host/port come from the config's ``server`` section; the worker starts via the app lifespan.
"""

from __future__ import annotations

import logging

import uvicorn

from orchestrator.app import _config_path, app
from orchestrator.config import load_orchestrator_config


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        cfg = load_orchestrator_config(_config_path())
        host, port = cfg.server.host, cfg.server.port
    except Exception:  # no config yet — fall back to defaults
        host, port = "0.0.0.0", 8000
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
