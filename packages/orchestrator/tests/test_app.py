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

    def is_alive(self) -> bool:
        return True

    def healthy(self) -> bool:
        return True


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
    assert client.get("/healthz").json() == {"status": "ok", "worker_alive": True}


def test_readyz_ok(client) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["status"] == "ready"


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


def test_worker_pause_resume_flow(client) -> None:
    panel = client.get("/ui/worker")
    assert panel.status_code == 200 and "Running" in panel.text
    # pause -> panel shows paused + (nothing queued) safe-to-redeploy
    assert client.post("/worker/pause", follow_redirects=False).status_code == 303
    paused = client.get("/ui/worker").text
    assert "Paused" in paused and "safe to redeploy" in paused
    # resume restores running
    assert client.post("/worker/resume", follow_redirects=False).status_code == 303
    assert "Running" in client.get("/ui/worker").text


def test_retry_dead_redirects(client) -> None:
    assert client.post("/jobs/retry-dead", follow_redirects=False).status_code == 303


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


def test_downloads_manual_add_edit_delete(client) -> None:
    # page renders
    assert client.get("/downloads").status_code == 200
    # add a manual game entry
    r = client.post("/downloads/add", data={
        "name": "Elden Ring", "kind": "game", "downloaded": "50 GB", "uploaded": "2 GB",
        "size": "50 GB", "completed_at": "2026-08-20", "notes": "steam",
    }, follow_redirects=False)
    assert r.status_code == 303
    # shows in the ledger table (all range) flagged Manual
    body = client.get("/ui/ledger", params={"range": "all"}).text
    assert "Elden Ring" in body and "Manual" in body
    # summary totals non-zero + chart renders
    assert client.get("/ui/ledger-summary", params={"range": "all"}).status_code == 200
    assert client.get("/ui/ledger-chart", params={"range": "all"}).status_code == 200
    # filter by type
    assert "Elden Ring" in client.get("/ui/ledger", params={"range": "all", "type": "game"}).text
    assert "Elden Ring" not in client.get("/ui/ledger", params={"range": "all", "type": "movie"}).text
    # find the row id via the services ledger and edit + delete it
    services = client.app.state.services
    rid = services.ledger.list()[0].id
    assert client.post(f"/downloads/{rid}/edit", data={
        "name": "Elden Ring GOTY", "kind": "game", "downloaded": "60 GB", "uploaded": "0",
        "completed_at": "2026-08-20", "notes": "",
    }, follow_redirects=False).status_code == 303
    assert services.ledger.get(rid).name == "Elden Ring GOTY"
    assert client.post(f"/downloads/{rid}/delete", follow_redirects=False).status_code == 303
    assert services.ledger.get(rid) is None


def test_downloads_unit_toggle(client) -> None:
    services = client.app.state.services
    services.ledger.add_manual(name="Big", kind="movie", downloaded_bytes=5 * 1024**3,
                               uploaded_bytes=1024**3, completed_at="2026-08-20")
    # add a cutoff with a limit so the remaining card renders too
    services.cutoffs.add(cutoff_date="2026-08-01", limit_bytes=100 * 1024**3, note="q")

    # default = GB suffix
    for ep in ("/ui/ledger", "/ui/ledger-summary", "/ui/ledger-chart", "/ui/ledger-remaining"):
        body = client.get(ep, params={"range": "all"}).text
        assert "GB" in body and "GiB" not in body, ep
    # units=iec = GiB suffix
    for ep in ("/ui/ledger", "/ui/ledger-summary", "/ui/ledger-chart", "/ui/ledger-remaining"):
        body = client.get(ep, params={"range": "all", "units": "iec"}).text
        assert "GiB" in body, ep


def test_downloads_custom_date_range(client) -> None:
    services = client.app.state.services
    services.ledger.add_manual(name="JanDL", kind="movie", downloaded_bytes=100,
                               uploaded_bytes=0, completed_at="2026-01-15T00:00:00+00:00")
    services.ledger.add_manual(name="MarDL", kind="movie", downloaded_bytes=200,
                               uploaded_bytes=0, completed_at="2026-03-15T00:00:00+00:00")
    # custom window catches only January
    jan = client.get("/ui/ledger", params={"start": "2026-01-01", "end": "2026-01-31"}).text
    assert "JanDL" in jan and "MarDL" not in jan
    # explicit dates override a preset range key
    both = client.get("/ui/ledger", params={"range": "7d", "start": "2026-01-01", "end": "2026-12-31"}).text
    assert "JanDL" in both and "MarDL" in both
    # summary respects the custom window
    assert client.get("/ui/ledger-summary", params={"start": "2026-01-01", "end": "2026-01-31"}).status_code == 200


def test_cutoffs_and_remaining(client) -> None:
    services = client.app.state.services
    # add ledger entries around a cutoff date
    services.ledger.add_manual(name="before", kind="movie", downloaded_bytes=10, uploaded_bytes=0,
                               completed_at="2026-07-01T00:00:00+00:00")
    services.ledger.add_manual(name="after", kind="game", downloaded_bytes=300, uploaded_bytes=100,
                               completed_at="2026-08-10T00:00:00+00:00")
    # add a cutoff (1000 B limit) on 2026-08-01 with a limit
    r = client.post("/cutoffs/add", data={"cutoff_date": "2026-08-01", "limit": "1000", "note": "Aug"},
                    follow_redirects=False)
    assert r.status_code == 303
    # cutoffs list shows it
    assert "2026-08-01" in client.get("/ui/cutoffs").text
    # downloads page marks 'Since cutoff' active
    dl = client.get("/downloads").text
    assert 'data-range="cutoff" class="chip active"' in dl
    # ledger default (no range) => only entries on/after cutoff (the 'after' one)
    led = client.get("/ui/ledger").text
    assert "after" in led and "before" not in led
    # remaining card: limit 1000 - used 400 (300+100) = 600 remaining, not over
    rem = client.get("/ui/ledger-remaining").text
    assert "600 B" in rem and "over" not in rem
    # remaining ignores the range param (still the cutoff period)
    assert "600 B" in client.get("/ui/ledger-remaining", params={"range": "7d"}).text


def test_remaining_over_limit_and_no_card(client) -> None:
    services = client.app.state.services
    services.ledger.add_manual(name="big", kind="movie", downloaded_bytes=2000, uploaded_bytes=0,
                               completed_at="2026-08-10T00:00:00+00:00")
    # no cutoff yet -> no card
    assert client.get("/ui/ledger-remaining").text.strip() == ""
    # cutoff with a 1000 B limit -> used 2000 -> over by 1000 (= 1.00 KB decimal)
    services.cutoffs.add(cutoff_date="2026-08-01", limit_bytes=1000, note="q")
    rem = client.get("/ui/ledger-remaining").text
    assert "over" in rem and "1.00 KB" in rem
    # cutoff without a limit -> no card
    services2 = client.app.state.services
    for c in services2.cutoffs.list():
        services2.cutoffs.delete(c.id)
    services2.cutoffs.add(cutoff_date="2026-08-05", limit_bytes=0, note="nolimit")
    assert client.get("/ui/ledger-remaining").text.strip() == ""


def test_ledger_type_column_has_icons(client) -> None:
    client.app.state.services.ledger.add_manual(name="EldenRing", kind="game",
                                                 downloaded_bytes=1, uploaded_bytes=0)
    body = client.get("/ui/ledger", params={"range": "all"}).text
    assert "🎮" in body and "EldenRing" in body
