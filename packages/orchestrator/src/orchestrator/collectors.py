"""Metric collectors. Each is best-effort — it must never raise into the worker loop (a metric
that can't be read is skipped, not fatal). Phase 1 ships the disk collector; container/system/
SMART collectors land in later phases behind the same ``(samples, evaluations)`` shape.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from mediacore.dashdot import DashdotClient, SystemSnapshot
from mediacore.docker_status import ContainerStatus, DockerStatusClient
from mediacore.scrutiny import DriveStatus, ScrutinyClient

from orchestrator.alerts import CRIT, OK, WARN, Evaluation
from orchestrator.config import AlertRule, DiskWatch, DockerSource
from orchestrator.metrics import Sample
from orchestrator.util import human_bytes, parse_bytes

log = logging.getLogger(__name__)


@dataclass
class DiskReport:
    label: str
    path: str
    total: int
    used: int
    free: int
    pct: float  # fill percentage 0..100
    severity: str


def _disk_severity(watch: DiskWatch, pct: float, free: int) -> str:
    """crit if the fill% OR the absolute-free threshold is in the crit band; else warn; else ok.
    Whichever threshold trips first wins (fuller disk / less free = worse)."""
    crit_free = parse_bytes(watch.crit_free) if watch.crit_free else 0
    warn_free = parse_bytes(watch.warn_free) if watch.warn_free else 0
    if (watch.crit_pct is not None and pct >= watch.crit_pct) or (crit_free and free <= crit_free):
        return CRIT
    if (watch.warn_pct is not None and pct >= watch.warn_pct) or (warn_free and free <= warn_free):
        return WARN
    return OK


def read_disk(watch: DiskWatch) -> DiskReport | None:
    """statvfs one watched path. Returns None if the path isn't reachable (unmounted, typo)."""
    try:
        usage = shutil.disk_usage(str(watch.path))
    except OSError as exc:
        log.warning("disk %s (%s) unreadable: %s", watch.label, watch.path, exc)
        return None
    pct = (usage.used / usage.total * 100) if usage.total else 0.0
    return DiskReport(
        label=watch.label,
        path=str(watch.path),
        total=usage.total,
        used=usage.used,
        free=usage.free,
        pct=pct,
        severity=_disk_severity(watch, pct, usage.free),
    )


def collect_disks(
    disks: list[DiskWatch], rule: AlertRule
) -> tuple[list[Sample], list[Evaluation]]:
    """Sample every watched disk. Returns ``(samples_for_storage, evaluations_for_alerting)``.
    Unreadable paths are skipped (best-effort)."""
    samples: list[Sample] = []
    evals: list[Evaluation] = []
    for watch in disks:
        rep = read_disk(watch)
        if rep is None:
            continue
        samples.append(Sample(f"disk.used:{rep.path}", float(rep.used)))
        samples.append(Sample(f"disk.free:{rep.path}", float(rep.free)))
        samples.append(Sample(f"disk.total:{rep.path}", float(rep.total)))
        samples.append(Sample(f"disk.pct:{rep.path}", round(rep.pct, 2)))
        detail = (
            f"{rep.label}: {rep.pct:.0f}% used · {human_bytes(rep.free)} free "
            f"of {human_bytes(rep.total)}"
        )
        evals.append(
            Evaluation(
                key=f"disk:{rep.path}",
                label=rep.label,
                severity=rep.severity,
                detail=detail,
                rule=rule,
            )
        )
    return samples, evals


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
    """Poll container state. Returns ``(samples, evaluations, containers)``. ``containers`` is the
    full list for the UI (None when the socket is unreachable — best-effort, no alert). Alerting is
    limited to ``cfg.watch`` (all containers when the list is empty)."""
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
        sev = _container_severity(c)
        detail = f"{c.name}: {c.status}" if c.status else f"{c.name}: {c.state}"
        evals.append(
            Evaluation(key=f"container:{c.name}", label=c.name, severity=sev, detail=detail, rule=rule)
        )
    return samples, evals, containers


def _scalar_severity(value: float, rule: AlertRule) -> str:
    """Severity for a higher-is-worse scalar (temp, mem %) against a rule's warn/crit."""
    if rule.crit is not None and value >= rule.crit:
        return CRIT
    if rule.warn is not None and value >= rule.warn:
        return WARN
    return OK


def collect_system(
    client: DashdotClient, temp_rule: AlertRule, mem_rule: AlertRule
) -> tuple[list[Sample], list[Evaluation], SystemSnapshot | None]:
    """Poll host system metrics via dashdot. Returns ``(samples, evaluations, snapshot)``; the
    snapshot is None when dashdot is unreachable (best-effort, no alert). Alerts on sustained
    CPU temperature and memory usage; CPU load / network are recorded + displayed only (load
    spikes during transcodes/prunes are normal, not alert-worthy)."""
    snap = client.snapshot()
    if snap is None:
        return [], [], None
    samples = [
        Sample("cpu.load", snap.cpu_load),
        Sample("mem.pct", round(snap.mem_pct, 2)),
        Sample("mem.used", float(snap.mem_used)),
        Sample("net.up", float(snap.net_up)),
        Sample("net.down", float(snap.net_down)),
    ]
    evals: list[Evaluation] = []
    if snap.cpu_temp is not None and temp_rule.enabled:
        samples.append(Sample("cpu.temp", snap.cpu_temp))
        evals.append(
            Evaluation(
                key="cpu_temp",
                label="CPU temp",
                severity=_scalar_severity(snap.cpu_temp, temp_rule),
                detail=f"CPU temperature {snap.cpu_temp:.0f}°C",
                rule=temp_rule,
            )
        )
    if mem_rule.enabled:
        evals.append(
            Evaluation(
                key="mem",
                label="Memory",
                severity=_scalar_severity(snap.mem_pct, mem_rule),
                detail=f"Memory {snap.mem_pct:.0f}% "
                f"({human_bytes(snap.mem_used)} of {human_bytes(snap.mem_total)})",
                rule=mem_rule,
            )
        )
    return samples, evals, snap


def collect_smart(
    client: ScrutinyClient, health_rule: AlertRule, temp_rule: AlertRule
) -> tuple[list[Sample], list[Evaluation], list[DriveStatus] | None]:
    """Poll drive SMART via Scrutiny. Returns ``(samples, evaluations, drives)``; drives is None
    when Scrutiny is unreachable. Alerts on a SMART health failure (crit) and sustained drive
    temperature."""
    drives = client.drives()
    if drives is None:
        return [], [], None
    samples: list[Sample] = []
    evals: list[Evaluation] = []
    for d in drives:
        if d.temp is not None:
            samples.append(Sample(f"smart.temp:{d.name}", float(d.temp)))
        if health_rule.enabled:
            evals.append(
                Evaluation(
                    key=f"smart:{d.name}",
                    label=f"{d.name} SMART",
                    severity=OK if d.passed else CRIT,
                    detail=f"{d.name} ({d.model}): SMART {'PASSED' if d.passed else 'FAILED'}",
                    rule=health_rule,
                )
            )
        if d.temp is not None and temp_rule.enabled:
            evals.append(
                Evaluation(
                    key=f"smart_temp:{d.name}",
                    label=f"{d.name} temp",
                    severity=_scalar_severity(d.temp, temp_rule),
                    detail=f"{d.name} drive temperature {d.temp}°C",
                    rule=temp_rule,
                )
            )
    return samples, evals, drives
