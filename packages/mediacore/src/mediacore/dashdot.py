"""Minimal dashdot API client (read-only, best-effort).

dashdot (https://github.com/MauriceNino/dashdot) already runs in the stack with privileged host
access, collecting CPU/RAM/network/temps. Rather than granting the orchestrator that same host
access, we read dashdot's plain REST endpoints:

* ``GET /info``          — static layout (cpu model, ram size, uptime) — pollable JSON
* ``GET /load/cpu``      — per-core ``{load, temp, core}``
* ``GET /load/ram``      — ``{load: <bytes used>}``
* ``GET /load/network``  — ``{up, down}`` bytes/sec

Like the other mediacore clients, every call is best-effort: failures log and return ``None``
rather than raising, so a monitoring hiccup never breaks the worker loop. Uses ``requests`` (an
existing dependency); no new deps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass
class SystemSnapshot:
    cpu_load: float  # average % across cores
    cpu_temp: float | None  # hottest core (°C), None if the host exposes no temps
    core_count: int
    mem_used: int
    mem_total: int
    net_up: int  # bytes/sec
    net_down: int  # bytes/sec
    uptime_s: float | None
    cpu_model: str

    @property
    def mem_pct(self) -> float:
        return (self.mem_used / self.mem_total * 100) if self.mem_total else 0.0


class DashdotClient:
    def __init__(self, base_url: str = "http://dash:3001", *, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str) -> Any | None:
        try:
            r = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("dashdot GET %s failed: %s", path, exc)
            return None

    def snapshot(self) -> SystemSnapshot | None:
        """Fetch a full system snapshot, or None if dashdot's core (CPU) endpoint is unreachable.
        Secondary endpoints are tolerated individually (their fields fall back to 0/None)."""
        cpu = self._get_json("/load/cpu")
        if not isinstance(cpu, list):
            return None  # dashdot unreachable / unexpected — treat the whole source as down
        ram = self._get_json("/load/ram") or {}
        net = self._get_json("/load/network") or {}
        info = self._get_json("/info") or {}

        cores = [c for c in cpu if isinstance(c, dict)]
        loads = [float(c.get("load") or 0) for c in cores]
        temps = [float(c["temp"]) for c in cores if c.get("temp")]
        cpu_info = info.get("cpu") or {}
        ram_info = info.get("ram") or {}
        os_info = info.get("os") or {}
        return SystemSnapshot(
            cpu_load=round(sum(loads) / len(loads), 1) if loads else 0.0,
            cpu_temp=max(temps) if temps else None,
            core_count=len(cores),
            mem_used=int(ram.get("load") or 0),
            mem_total=int(ram_info.get("size") or 0),
            net_up=int(net.get("up") or 0),
            net_down=int(net.get("down") or 0),
            uptime_s=os_info.get("uptime"),
            cpu_model=str(cpu_info.get("model") or ""),
        )
