"""Per-device WAN usage, polled from the router's own statistics table.

Unlike the other sources this one *writes* as it collects: the router reports cumulative counters
that reset without warning, so the durable per-day record is built by differencing consecutive
polls (see :mod:`orchestrator.wan_usage`). Losing a poll therefore loses at most that interval,
not the period.

The pause gate is not a nicety. The router accepts exactly one admin session, so polling it
evicts whoever is logged into its web UI. Pausing is stored as a *deadline* rather than a flag so
it resumes itself — a pause left on by accident would silently stop the household's usage being
recorded, and unlike the worker pause there is nothing visibly stuck to remind you.
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from mediacore.router import DeviceStats, RouterClient

from orchestrator.alerts import Evaluation
from orchestrator.config import MonitoringConfig
from orchestrator.metrics import Sample
from orchestrator.monitor.base import Collected
from orchestrator.wan_usage import PollResult, WanUsage, display_name

log = logging.getLogger(__name__)

#: durable key holding an ISO timestamp; polling is skipped until then.
PAUSE_KEY = "wan_poll_paused_until"


@dataclass
class WanDeps:
    """What this source needs beyond its config — the durable store and the pause flag.

    Passed in rather than reached for so :func:`orchestrator.monitor.build_sources` stays a pure
    function of config for every other source, and so this module never imports the job queue.
    """

    usage: WanUsage
    get_meta: Callable[[str], str | None]


@dataclass
class WanSnapshot:
    """What the UI renders. ``paused_until`` set means the poll was deliberately skipped.

    Kept distinct from the source being unreachable: ``ok`` is still ``True`` while paused,
    because the router is presumably fine and saying "unreachable" would be a lie that sends
    someone debugging their network.
    """

    devices: list[DeviceStats] = field(default_factory=list)
    paused_until: _dt.datetime | None = None
    last_poll: str | None = None
    result: PollResult | None = None

    @property
    def paused(self) -> bool:
        return self.paused_until is not None

    @property
    def rate_rx(self) -> int:
        """Live household download rate, from the router's current sampling interval."""
        return sum(d.curr_rx for d in self.devices)

    @property
    def rate_tx(self) -> int:
        return sum(d.curr_tx for d in self.devices)


def paused_until(get_meta: Callable[[str], str | None]) -> _dt.datetime | None:
    """The pause deadline if one is set and still in the future, else None."""
    raw = get_meta(PAUSE_KEY)
    if not raw:
        return None
    try:
        deadline = _dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return deadline if deadline > _dt.datetime.now(_dt.UTC) else None


class WanMonitor:
    name = "wan"

    def __init__(self, mon: MonitoringConfig, deps: WanDeps | None = None) -> None:
        self.mon = mon
        self.deps = deps
        cfg = mon.router
        self.client = (
            RouterClient(cfg.url, cfg.username, cfg.password)
            if cfg.enabled and cfg.password and deps is not None
            else None
        )
        self._settings_checked = False
        if self.enabled:
            # Logged because a bound set too generously is invisible otherwise — it produces no
            # rejections, no warnings, and no symptom until a bogus counter slips under it.
            log.info(
                "wan: plausibility bound %d B/s -> %.0f MB per %ds poll",
                cfg.link_max_bytes_s,
                cfg.interval_s * cfg.link_max_bytes_s * 1.5 / 1e6,
                cfg.interval_s,
            )

    @property
    def enabled(self) -> bool:
        return self.mon.router.enabled and self.client is not None

    @property
    def interval_s(self) -> int:
        return self.mon.router.interval_s

    def collect(self) -> Collected:
        assert self.client is not None and self.deps is not None  # enabled() checked
        until = paused_until(self.deps.get_meta)
        if until is not None:
            # Deliberately no network call: the whole point of pausing is to release the session.
            return [], [], WanSnapshot(paused_until=until)

        if self.mon.router.manage_settings and not self._settings_checked:
            # Once per process, and a no-op write-wise unless the router really has it off.
            self._settings_checked = self.client.ensure_stats_enabled()

        devices = self.client.devices()
        if devices is None:
            return [], [], None  # unreachable -> ok=False, stale numbers stay off the screen

        result = self.deps.usage.apply(
            devices, link_max_bytes_s=self.mon.router.link_max_bytes_s
        )
        aliases = self.mon.router.device_aliases
        if aliases:
            # display_name, not a lookup written out again here: the Bandwidth tab groups its
            # stored rows by the same function, and the live "Now" column is joined to those groups
            # by name. Two spellings of "what is this device called" would silently stop matching.
            devices = [
                DeviceStats(
                    mac=d.mac, host=display_name(aliases, d.mac, d.host),
                    kind=d.kind, rx=d.rx, tx=d.tx, curr_rx=d.curr_rx, curr_tx=d.curr_tx,
                )
                for d in devices
            ]
        snapshot = WanSnapshot(
            devices=devices,
            last_poll=_dt.datetime.now(_dt.UTC).isoformat(timespec="seconds"),
            result=result,
        )
        # Aggregate only: a sample per device per minute would multiply this table's growth by the
        # size of the household, and per-device history already lives in wan_usage.
        samples = [
            Sample("wan.rx", float(snapshot.rate_rx)),
            Sample("wan.tx", float(snapshot.rate_tx)),
        ]
        evals: list[Evaluation] = []
        return samples, evals, snapshot
