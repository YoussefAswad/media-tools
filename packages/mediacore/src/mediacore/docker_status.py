"""Minimal Docker Engine API client over the local UNIX socket (read-only, best-effort).

Lists containers and their up/down + health state for the Monitoring tab. Like the other
mediacore clients (:class:`~mediacore.notify.Notifier`, :class:`~mediacore.qbit.QbitClient`,
:class:`~mediacore.jellyfin.JellyfinClient`), every call is best-effort: on any failure it logs
and returns ``None`` rather than raising, so a monitoring hiccup never breaks the worker loop.

Uses only the standard library — ``http.client`` over an ``AF_UNIX`` socket — so it needs no
Docker SDK dependency, keeping with mediacore/mkvprune's lean-deps convention. The container
must have the socket bind-mounted (``/var/run/docker.sock``) and, running unprivileged, be a
member of the socket's group.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: health verdict embedded in the Docker "Status" string, e.g. "Up 3 hours (healthy)".
_HEALTH_RE = re.compile(r"\((healthy|unhealthy|health: starting)\)")


class _UnixHTTPConnection(http.client.HTTPConnection):
    """An HTTPConnection that dials a UNIX-domain socket instead of TCP."""

    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


@dataclass
class ContainerStatus:
    name: str
    state: str  # running | exited | created | restarting | paused | dead
    status: str  # human string, e.g. "Up 21 hours (healthy)"
    health: str | None  # healthy | unhealthy | starting | None (no healthcheck)
    up: bool
    id: str = ""
    #: filled in by :meth:`DockerStatusClient.enrich_stats` for running containers.
    cpu_pct: float | None = None
    mem_used: int | None = None
    mem_limit: int | None = None


def _compute_stats(d: dict) -> tuple[float, int, int] | None:
    """CPU % (of total host capacity) + used/limit memory from a ``/stats?stream=false`` body.
    The single response carries both ``cpu_stats`` and ``precpu_stats`` (~1s apart), so one call
    yields a valid CPU delta. Memory subtracts ``inactive_file`` to match ``docker stats``."""
    try:
        cpu, pre = d["cpu_stats"], d["precpu_stats"]
        cpu_delta = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or [1]) or 1
        cpu_pct = (cpu_delta / sys_delta) * ncpu * 100.0 if sys_delta > 0 and cpu_delta > 0 else 0.0
        mem = d["memory_stats"]
        inactive = (mem.get("stats") or {}).get("inactive_file", 0)
        mem_used = max(0, mem.get("usage", 0) - inactive)
        return round(cpu_pct, 1), int(mem_used), int(mem.get("limit", 0))
    except (KeyError, TypeError, ZeroDivisionError):
        return None


class DockerStatusClient:
    def __init__(self, socket_path: str = "/var/run/docker.sock", *, timeout: float = 5.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def containers(self) -> list[ContainerStatus] | None:
        """Return every container (running or not) with its state + health, or None if the socket
        is unreachable / the API errors. Sorted by name for a stable UI order."""
        body = self._get("/containers/json?all=1")
        if body is None:
            return None
        try:
            data = json.loads(body)
        except ValueError:
            log.warning("docker API returned non-JSON")
            return None
        out = [self._parse(c) for c in data if isinstance(c, dict)]
        out.sort(key=lambda c: c.name)
        return out

    def stats(self, container_id: str) -> tuple[float, int, int] | None:
        """(cpu_pct, mem_used, mem_limit) for one container, or None. ~1s per call (Docker samples
        over a window even with stream=false) — fetch in parallel for many, see enrich_stats."""
        body = self._get(f"/containers/{container_id}/stats?stream=false")
        if body is None:
            return None
        try:
            return _compute_stats(json.loads(body))
        except ValueError:
            return None

    def enrich_stats(self, containers: list[ContainerStatus], *, max_workers: int = 16) -> None:
        """Fill cpu/mem on each *running* container, fetching stats concurrently (socket-only, no
        shared state) so the whole set costs ~one stats call's latency instead of N. Best-effort:
        a container whose stats fail is left with None fields."""
        running = [c for c in containers if c.up and c.id]
        if not running:
            return

        def _fill(c: ContainerStatus) -> None:
            s = self.stats(c.id)
            if s is not None:
                c.cpu_pct, c.mem_used, c.mem_limit = s

        with ThreadPoolExecutor(max_workers=min(max_workers, len(running))) as ex:
            list(ex.map(_fill, running))

    def _get(self, path: str) -> bytes | None:
        try:
            conn = _UnixHTTPConnection(self.socket_path, self.timeout)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
        except OSError as exc:
            log.warning("docker socket %s unreachable: %s", self.socket_path, exc)
            return None
        if resp.status != 200:
            log.warning("docker API %s -> %s", path, resp.status)
            return None
        return body

    @staticmethod
    def _parse(c: dict) -> ContainerStatus:
        names = c.get("Names") or []
        name = (names[0] if names else (c.get("Id") or "")[:12]).lstrip("/")
        state = c.get("State") or ""
        status = c.get("Status") or ""
        m = _HEALTH_RE.search(status)
        health = None
        if m:
            token = m.group(1)
            health = "starting" if token.startswith("health:") else token
        return ContainerStatus(
            name=name,
            state=state,
            status=status,
            health=health,
            up=(state == "running"),
            id=c.get("Id") or "",
        )
