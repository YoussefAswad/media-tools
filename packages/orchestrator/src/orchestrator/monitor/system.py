"""Host CPU / RAM / temps / network, read from the dashdot container's REST API."""

from __future__ import annotations

from mediacore.dashdot import DashdotClient, SystemSnapshot

from orchestrator.alerts import Evaluation
from orchestrator.config import AlertRule, MonitoringConfig
from orchestrator.metrics import Sample
from orchestrator.monitor.base import Collected, scalar_severity


def collect_system(
    client: DashdotClient, temp_rule: AlertRule, mem_rule: AlertRule
) -> tuple[list[Sample], list[Evaluation], SystemSnapshot | None]:
    """Alerts on sustained CPU temperature and memory usage; CPU load and network are recorded
    and displayed only — load spikes during a transcode or prune are normal, not alert-worthy."""
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
                severity=scalar_severity(snap.cpu_temp, temp_rule),
                detail=f"CPU temperature {snap.cpu_temp:.0f}°C",
                rule=temp_rule,
            )
        )
    if mem_rule.enabled:
        from orchestrator.util import human_bytes

        evals.append(
            Evaluation(
                key="mem",
                label="Memory",
                severity=scalar_severity(snap.mem_pct, mem_rule),
                detail=f"Memory {snap.mem_pct:.0f}% "
                f"({human_bytes(snap.mem_used)} of {human_bytes(snap.mem_total)})",
                rule=mem_rule,
            )
        )
    return samples, evals, snap


class SystemMonitor:
    name = "system"

    def __init__(self, mon: MonitoringConfig) -> None:
        self.mon = mon
        self.client = DashdotClient(mon.system.url) if mon.system.enabled else None

    @property
    def enabled(self) -> bool:
        return self.mon.system.enabled and self.client is not None

    @property
    def interval_s(self) -> int:
        return self.mon.system.interval_s

    def collect(self) -> Collected:
        assert self.client is not None  # enabled() checked
        return collect_system(self.client, self.mon.cpu_temp_alert, self.mon.mem_alert)
