"""Monitored sources: disks, containers, host system, drive SMART, per-device WAN usage.

Each source declares its own name, enabled-ness and interval, and knows how to collect itself.
The worker loops over :func:`build_sources` with one generic interval gate; the web routes read
the resulting snapshots rather than the socket or API behind them.
"""

from orchestrator.config import MonitoringConfig
from orchestrator.monitor.base import (
    MISSING,
    Collected,
    MonitorSource,
    SourceSnapshot,
    scalar_severity,
)
from orchestrator.monitor.disk import DiskMonitor, DiskReport, collect_disks, read_disk
from orchestrator.monitor.docker import ContainerMonitor, collect_containers
from orchestrator.monitor.smart import SmartMonitor, collect_smart
from orchestrator.monitor.system import SystemMonitor, collect_system
from orchestrator.monitor.wan import WanDeps, WanMonitor, WanSnapshot, paused_until


def build_sources(
    mon: MonitoringConfig, *, wan: WanDeps | None = None
) -> list[MonitorSource]:
    """Every source, enabled or not. A disabled one is kept in the list so the UI can tell
    "switched off" apart from "never collected" without a second lookup.

    ``wan`` carries the one thing a source cannot get from config: the WAN source both writes a
    durable record and reads a durable pause flag. Omitting it leaves that source switched off.
    """
    return [
        DiskMonitor(mon),
        ContainerMonitor(mon),
        SystemMonitor(mon),
        SmartMonitor(mon),
        WanMonitor(mon, wan),
    ]


__all__ = [
    "MISSING",
    "Collected",
    "ContainerMonitor",
    "DiskMonitor",
    "DiskReport",
    "MonitorSource",
    "SmartMonitor",
    "SourceSnapshot",
    "SystemMonitor",
    "WanDeps",
    "WanMonitor",
    "WanSnapshot",
    "build_sources",
    "collect_containers",
    "collect_disks",
    "collect_smart",
    "collect_system",
    "paused_until",
    "read_disk",
    "scalar_severity",
]
