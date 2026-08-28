"""Watched filesystems, read in-container with statvfs."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass

from orchestrator.alerts import CRIT, OK, WARN, Evaluation
from orchestrator.config import AlertRule, DiskWatch, MonitoringConfig
from orchestrator.metrics import Sample
from orchestrator.monitor.base import Collected
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
    """statvfs one watched path. Returns None if the path isn't reachable (unmounted, typo).

    Cheap enough that the disk routes call it live rather than reading the snapshot — one
    statvfs, versus a socket round-trip for the other sources.
    """
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


def collect_disks(disks: list[DiskWatch], rule: AlertRule) -> tuple[list[Sample], list[Evaluation]]:
    """Sample every watched disk. Unreadable paths are skipped (best-effort)."""
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


class DiskMonitor:
    """Always on when monitoring is on: it needs no external service, just the mounts."""

    name = "disk"

    def __init__(self, mon: MonitoringConfig) -> None:
        self.mon = mon

    @property
    def enabled(self) -> bool:
        return True

    @property
    def interval_s(self) -> int:
        return self.mon.disk_interval_s

    def collect(self) -> Collected:
        samples, evals = collect_disks(self.mon.disks, self.mon.disk_alert)
        reports = [r for r in (read_disk(d) for d in self.mon.disks) if r is not None]
        return samples, evals, reports
