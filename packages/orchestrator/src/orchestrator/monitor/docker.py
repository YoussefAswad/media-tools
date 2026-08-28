"""Container up/down via the Docker Engine API over the local UNIX socket."""

from __future__ import annotations

from mediacore.docker_status import ContainerStatus, DockerStatusClient

from orchestrator.alerts import CRIT, OK, WARN, Evaluation
from orchestrator.config import AlertRule, DockerSource, MonitoringConfig
from orchestrator.metrics import Sample
from orchestrator.monitor.base import Collected


def _container_severity(c: ContainerStatus) -> str:
    """down = crit; up-but-unhealthy = warn; up (healthy / starting / no healthcheck) = ok."""
    if not c.up:
        return CRIT
    if c.health == "unhealthy":
        return WARN
    return OK


def collect_containers(
    client: DockerStatusClient, cfg: DockerSource, rule: AlertRule
) -> tuple[list[Sample], list[Evaluation], list[ContainerStatus] | None]:
    """Poll container state. ``containers`` is None when the socket is unreachable (best-effort,
    no alert). Alerting is limited to ``cfg.watch`` (all containers when the list is empty)."""
    containers = client.containers()
    if containers is None:
        return [], [], None
    up = sum(1 for c in containers if c.up)
    samples = [
        Sample("docker.containers_up", float(up)),
        Sample("docker.containers_total", float(len(containers))),
    ]
    watch = set(cfg.watch)
    evals: list[Evaluation] = []
    for c in containers:
        if watch and c.name not in watch:
            continue
        detail = f"{c.name}: {c.status}" if c.status else f"{c.name}: {c.state}"
        evals.append(
            Evaluation(
                key=f"container:{c.name}",
                label=c.name,
                severity=_container_severity(c),
                detail=detail,
                rule=rule,
            )
        )
    return samples, evals, containers


class ContainerMonitor:
    name = "docker"

    def __init__(self, mon: MonitoringConfig) -> None:
        self.mon = mon
        self.client = DockerStatusClient(str(mon.docker.socket)) if mon.docker.enabled else None

    @property
    def enabled(self) -> bool:
        return self.mon.docker.enabled and self.client is not None

    @property
    def interval_s(self) -> int:
        return self.mon.docker.interval_s

    def collect(self) -> Collected:
        assert self.client is not None  # enabled() checked
        samples, evals, containers = collect_containers(
            self.client, self.mon.docker, self.mon.container_alert
        )
        if containers is not None and self.mon.docker.stats:
            # Per-container CPU/mem for the UI list (~1s each, so parallel and running-only).
            # This is why the routes read the snapshot instead of polling the socket per request.
            self.client.enrich_stats(containers)
        return samples, evals, containers
