"""Minimal Scrutiny API client (read-only, best-effort).

Scrutiny (https://github.com/AnalogJ/scrutiny) runs its own privileged collector that polls disk
SMART data via smartctl and stores it. Rather than granting the orchestrator raw device access +
SYS_RAWIO, we read Scrutiny's web API:

* ``GET /api/summary`` — per-device: ``{device: {device_name, model_name, device_status}, smart:
  {temp, power_on_hours}}``. ``device_status`` is 0 when the drive passed (non-zero = failed).

Like the other mediacore clients, every call is best-effort: failures log and return ``None``
rather than raising. Reading Scrutiny's cached summary is cheap and does not spin the disks — the
collector does that on its own (slow) schedule. Uses ``requests`` (an existing dep).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)


@dataclass
class DriveStatus:
    name: str  # device_name, e.g. "sda"
    model: str
    passed: bool  # SMART/scrutiny health verdict
    temp: int | None  # °C
    power_on_hours: int | None


class ScrutinyClient:
    def __init__(self, base_url: str = "http://scrutiny:8080", *, timeout: float = 8.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get_json(self, path: str) -> Any | None:
        try:
            r = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("scrutiny GET %s failed: %s", path, exc)
            return None

    def drives(self) -> list[DriveStatus] | None:
        """Per-drive SMART status, or None if Scrutiny is unreachable / returns an unexpected body.
        Sorted by device name for a stable UI order."""
        data = self._get_json("/api/summary")
        if not isinstance(data, dict):
            return None
        # shape: {"success": true, "data": {"summary": {"<wwn>": {device, smart, ...}}}}
        payload = (data.get("data") or {}).get("summary")
        if not isinstance(payload, dict):
            return None
        out: list[DriveStatus] = []
        for wwn, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            dev = entry.get("device") or {}
            smart = entry.get("smart") or {}
            # device_status: 0 = passed; non-zero is a failure bitmask (smart / scrutiny).
            passed = not dev.get("device_status", 0)
            temp = smart.get("temp")
            poh = smart.get("power_on_hours")
            out.append(
                DriveStatus(
                    name=dev.get("device_name") or str(wwn),
                    model=dev.get("model_name") or "",
                    passed=passed,
                    temp=int(temp) if isinstance(temp, (int, float)) else None,
                    power_on_hours=int(poh) if isinstance(poh, (int, float)) else None,
                )
            )
        out.sort(key=lambda d: d.name)
        return out
