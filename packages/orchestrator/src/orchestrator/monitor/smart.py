"""Drive SMART health + temperature, read from a Scrutiny sidecar's cached summary."""

from __future__ import annotations

from mediacore.scrutiny import DriveStatus, ScrutinyClient

from orchestrator.alerts import CRIT, OK, Evaluation
from orchestrator.config import AlertRule, MonitoringConfig
from orchestrator.metrics import Sample
from orchestrator.monitor.base import Collected, scalar_severity


def collect_smart(
    client: ScrutinyClient, health_rule: AlertRule, temp_rule: AlertRule
) -> tuple[list[Sample], list[Evaluation], list[DriveStatus] | None]:
    """``drives`` is None when Scrutiny is unreachable. Alerts on a SMART health failure (crit,
    no sustain) and on sustained drive temperature."""
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
                    severity=scalar_severity(d.temp, temp_rule),
                    detail=f"{d.name} drive temperature {d.temp}°C",
                    rule=temp_rule,
                )
            )
    return samples, evals, drives


class SmartMonitor:
    name = "smart"

    def __init__(self, mon: MonitoringConfig) -> None:
        self.mon = mon
        self.client = ScrutinyClient(mon.smart.url) if mon.smart.enabled else None

    @property
    def enabled(self) -> bool:
        return self.mon.smart.enabled and self.client is not None

    @property
    def interval_s(self) -> int:
        return self.mon.smart.interval_s

    def collect(self) -> Collected:
        assert self.client is not None  # enabled() checked
        return collect_smart(self.client, self.mon.smart_alert, self.mon.smart_temp_alert)
