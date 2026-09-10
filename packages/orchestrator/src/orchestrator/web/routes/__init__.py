"""One router per tab, mounted by :mod:`orchestrator.app`.

Split out of a single 1 000-line ``app.py`` that held 48 routes plus the presentation helpers.
Every ``/ui/*`` route renders a partial for a polling panel; every mutation posts and redirects.
Auth is declared once per router rather than on each route.
"""

from orchestrator.web.routes import (
    config,
    dashboard,
    downloads,
    health,
    jobs,
    library,
    monitoring,
    quota,
    webhooks,
)

#: mounted in this order; paths are absolute so the order is cosmetic.
ROUTERS = [
    health.router,
    webhooks.router,
    dashboard.router,
    jobs.router,
    library.router,
    downloads.router,
    quota.router,
    monitoring.router,
    config.router,
]

__all__ = ["ROUTERS"]
