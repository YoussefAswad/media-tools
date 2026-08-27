from __future__ import annotations

import json

from mediacore.docker_status import DockerStatusClient

_SAMPLE = [
    {"Names": ["/radarr"], "State": "running", "Status": "Up 21 hours"},
    {"Names": ["/ntfy"], "State": "running", "Status": "Up 21 hours (healthy)"},
    {"Names": ["/yamtrack"], "State": "running", "Status": "Up 21 hours (unhealthy)"},
    {"Names": ["/booting"], "State": "running", "Status": "Up 2 seconds (health: starting)"},
    {"Names": ["/oldjob"], "State": "exited", "Status": "Exited (0) 3 hours ago"},
]


def test_parse_state_and_health() -> None:
    by_name = {c.name: c for c in (DockerStatusClient._parse(c) for c in _SAMPLE)}
    assert by_name["radarr"].up and by_name["radarr"].health is None
    assert by_name["ntfy"].health == "healthy"
    assert by_name["yamtrack"].health == "unhealthy"
    assert by_name["booting"].health == "starting"
    assert not by_name["oldjob"].up


def test_containers_sorted_and_parsed(monkeypatch) -> None:
    client = DockerStatusClient("/nonexistent.sock")
    monkeypatch.setattr(client, "_get", lambda path: json.dumps(_SAMPLE).encode())
    cs = client.containers()
    assert cs is not None
    assert [c.name for c in cs] == sorted(c.name for c in cs)  # stable order
    assert len(cs) == 5


def test_containers_unreachable_returns_none(monkeypatch) -> None:
    client = DockerStatusClient("/nonexistent.sock")
    monkeypatch.setattr(client, "_get", lambda path: None)
    assert client.containers() is None


def test_containers_bad_json_returns_none(monkeypatch) -> None:
    client = DockerStatusClient("/nonexistent.sock")
    monkeypatch.setattr(client, "_get", lambda path: b"not json")
    assert client.containers() is None


def test_get_on_dead_socket_is_best_effort() -> None:
    # a real connect() to a missing socket must be swallowed (OSError -> None), never raised.
    client = DockerStatusClient("/no/such/docker.sock", timeout=0.5)
    assert client.containers() is None
