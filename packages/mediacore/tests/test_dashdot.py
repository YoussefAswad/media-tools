from __future__ import annotations

from mediacore.dashdot import DashdotClient, SystemSnapshot

_CPU = [
    {"load": 10.0, "temp": 70.0, "core": 0},
    {"load": 30.0, "temp": 75.6, "core": 1},
]
_RAM = {"load": 8_000_000_000}
_NET = {"up": 1_000, "down": 12_000}
_INFO = {
    "cpu": {"model": "Ryzen 7 5800H"},
    "ram": {"size": 16_000_000_000},
    "os": {"uptime": 944074.0},
}


def _client(monkeypatch, routes):
    client = DashdotClient("http://dash:3001")
    monkeypatch.setattr(client, "_get_json", lambda path: routes.get(path))
    return client


def test_snapshot_full(monkeypatch) -> None:
    client = _client(
        monkeypatch,
        {"/load/cpu": _CPU, "/load/ram": _RAM, "/load/network": _NET, "/info": _INFO},
    )
    s = client.snapshot()
    assert s is not None
    assert s.cpu_load == 20.0  # avg of 10 + 30
    assert s.cpu_temp == 75.6  # hottest core
    assert s.core_count == 2
    assert s.mem_used == 8_000_000_000 and s.mem_total == 16_000_000_000
    assert round(s.mem_pct, 1) == 50.0
    assert s.net_up == 1_000 and s.net_down == 12_000
    assert s.uptime_s == 944074.0
    assert s.cpu_model == "Ryzen 7 5800H"


def test_snapshot_unreachable_when_cpu_missing(monkeypatch) -> None:
    client = _client(monkeypatch, {"/load/cpu": None})
    assert client.snapshot() is None


def test_snapshot_tolerates_missing_secondary(monkeypatch) -> None:
    # cpu present but ram/net/info fail → snapshot still returns, with fallbacks
    client = _client(monkeypatch, {"/load/cpu": _CPU})
    s = client.snapshot()
    assert s is not None
    assert s.mem_total == 0 and s.mem_pct == 0.0
    assert s.cpu_temp == 75.6
    assert s.uptime_s is None


def test_snapshot_no_temps(monkeypatch) -> None:
    cpu = [{"load": 5.0, "core": 0}, {"load": 5.0, "core": 1}]  # no temp field
    client = _client(monkeypatch, {"/load/cpu": cpu, "/info": _INFO})
    s = client.snapshot()
    assert s is not None and s.cpu_temp is None


def test_mem_pct_zero_total() -> None:
    s = SystemSnapshot(0, None, 0, 100, 0, 0, 0, None, "")
    assert s.mem_pct == 0.0  # no division by zero
