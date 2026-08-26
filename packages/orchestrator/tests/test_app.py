from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


class DummyWorker:
    def __init__(self, *a, **k) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
audit:
  state_dir: {tmp_path / "state"}
arr:
  radarr:
    base_url: http://radarr:7878
    api_key: k
ntfy:
  status_granularity: "off"
webhook_secret: topsecret
enable_seasonal_shift: false
enable_quality_check: false
enable_prune: false
"""
    )
    return cfg


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(_write_config(tmp_path)))
    import orchestrator.app as appmod

    monkeypatch.setattr(appmod, "Worker", DummyWorker)
    with TestClient(appmod.app) as c:
        yield c


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_webhook_test_event(client) -> None:
    r = client.post(
        "/arr/radarr",
        json={"eventType": "Test"},
        headers={"X-Webhook-Token": "topsecret"},
    )
    assert r.status_code == 200 and r.json()["test"] is True


def test_webhook_bad_secret(client) -> None:
    r = client.post("/arr/radarr", json={"eventType": "Test"}, headers={"X-Webhook-Token": "nope"})
    assert r.status_code == 401


def test_webhook_download_enqueues(client) -> None:
    r = client.post(
        "/arr/radarr",
        json={
            "eventType": "Download",
            "movie": {"id": 5, "title": "Mov", "year": 2021},
            "movieFile": {"path": "/data/movies/Mov/f.mkv"},
        },
        headers={"X-Webhook-Token": "topsecret"},
    )
    assert r.status_code == 200 and r.json()["status"] == "queued"
    # job shows up in the active panel, with a Movie type + searchable + app-filterable
    active = client.get("/ui/active")
    assert "Mov (2021)" in active.text and "Movie" in active.text
    assert "Mov (2021)" in client.get("/ui/active", params={"q": "Mov"}).text
    assert "Mov (2021)" not in client.get("/ui/active", params={"q": "zzz"}).text
    assert "Mov (2021)" in client.get("/ui/active", params={"app": "radarr"}).text
    assert "Mov (2021)" not in client.get("/ui/active", params={"app": "sonarr"}).text


def test_summary_and_clear_dead(client) -> None:
    assert client.get("/ui/summary").status_code == 200
    # clear-dead redirects and succeeds even with nothing to clear
    r = client.post("/jobs/clear-dead", follow_redirects=False)
    assert r.status_code == 303


def test_dashboard_and_config_pages(client) -> None:
    assert client.get("/").status_code == 200
    cfg = client.get("/config")
    assert cfg.status_code == 200 and "Configuration" in cfg.text


def test_config_save_invalid_rejected(client) -> None:
    r = client.post("/config", data={"raw": "server: {port: not-an-int}"})
    assert r.status_code == 400


def test_config_save_valid_reloads(client, tmp_path) -> None:
    good = "audit:\n  state_dir: %s\nenable_prune: false\n" % (tmp_path / "state")
    r = client.post("/config", data={"raw": good})
    assert r.status_code == 200 and "Saved" in r.text
