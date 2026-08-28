"""Monitored sources: disks, containers, host system, drive SMART.

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


def build_sources(mon: MonitoringConfig) -> list[MonitorSource]:
    """Every source, enabled or not. A disabled one is kept in the list so the UI can tell
    "switched off" apart from "never collected" without a second lookup."""
    return [DiskMonitor(mon), ContainerMonitor(mon), SystemMonitor(mon), SmartMonitor(mon)]


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
    "build_sources",
    "collect_containers",
    "collect_disks",
    "collect_smart",
    "collect_system",
    "read_disk",
    "scalar_severity",
]
