"""The Quota tab end to end: routes, the config write-back, and the ledger's attribution form."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from mediacore.router import DeviceStats

CONFIG = """
audit:
  state_dir: {state}
webhook_secret: topsecret
enable_seasonal_shift: false
enable_quality_check: false
enable_prune: false
enable_ledger: false
ntfy:
  status_granularity: "off"
quotas:
  server_device: Linux-G15
  games_device: PC-114
  quotas:
    youssef: {{allocation: 300 GB}}
    household: {{label: Household, shared: true}}
  people:
    youssef: {{}}
    logain: {{}}
  devices:
    "Linux-G15": "youssef"
    "PC-114": "youssef"
    "Logains-iPhone": "logain"
"""

GB = 1_000_000_000


class DummyWorker:
    def __init__(self, *a, **kw) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def join(self, timeout=None) -> None: ...
    def is_alive(self) -> bool:
        return True

    def healthy(self) -> bool:
        return True


@pytest.fixture()
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG.format(state=tmp_path / "state"))
    return p


@pytest.fixture()
def client(tmp_path, cfg_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(cfg_path))
    import orchestrator.app as appmod

    monkeypatch.setattr(appmod, "Worker", DummyWorker)
    with TestClient(appmod.app) as c:
        yield c


def _services(client):
    return client.app.state.services


def _record_wan(client, **totals: int) -> None:
    """Two polls per device, so the delta engine has a difference it observed to count."""
    wan = _services(client).wan
    devices = [
        DeviceStats(mac=f"AA:{i:02X}", host=host, kind="wired", rx=0, tx=0, curr_rx=0, curr_tx=0)
        for i, host in enumerate(totals)
    ]
    wan.apply(devices, link_max_bytes_s=10**12)
    wan.apply(
        [
            DeviceStats(
                mac=d.mac, host=d.host, kind=d.kind, rx=totals[d.host], tx=0, curr_rx=0, curr_tx=0
            )
            for d in devices
        ],
        link_max_bytes_s=10**12,
    )


# --- the tab ------------------------------------------------------------------------------


def test_the_tab_renders_and_is_linked_from_the_nav(client) -> None:
    page = client.get("/quota")
    assert page.status_code == 200
    assert 'href="/quota"' in client.get("/").text


def test_quotas_panel_shows_each_bucket_and_reconciles(client) -> None:
    _record_wan(client, **{"Linux-G15": 100 * GB, "Logains-iPhone": 50 * GB, "TCL-C6K": 10 * GB})
    html = client.get("/ui/quota-buckets").text
    assert "Youssef" in html and "Logain" in html and "Household" in html
    assert "nothing unattributed" in html
    assert "Attribution mismatch" not in html


def test_a_quota_with_no_allocation_says_so_rather_than_showing_a_bar(client) -> None:
    """Unbounded is not empty — a green bar would imply a limit it is comfortably inside."""
    _record_wan(client, **{"TCL-C6K": 10 * GB})
    html = client.get("/ui/quota-buckets").text
    assert "no allocation set" in html
    assert "% of household" in html


def test_an_unowned_device_is_listed_for_assignment(client) -> None:
    _record_wan(client, **{"Xiaomi-TV-Box": 3 * GB})
    html = client.get("/ui/quota-unassigned").text
    assert "Xiaomi-TV-Box" in html
    assert '<form method="post" action="/quota/assign"' in html


def test_people_are_listed_by_name_not_by_their_quota(client) -> None:
    """Labelling people by quota collapsed everyone pointed at the shared pot into one entry.

    Three people all reading "Household" in a dropdown you pick an *owner* from is unusable, and
    it shipped once — spotted on the live tab, not in a test.
    """
    services = _services(client)
    services.config.quotas.people = {
        "logain": {"quota": "household"},      # type: ignore[dict-item]
        "joury": {"quota": "household"},       # type: ignore[dict-item]
        "youssef": {},                          # type: ignore[dict-item]
    }
    _record_wan(client, **{"Xiaomi-TV-Box": GB})
    html = client.get("/ui/quota-unassigned").text
    assert "Logain &#8594; Household" in html or "Logain \u2192 Household" in html
    assert "Joury" in html
    # the person who is their own quota needs no arrow
    assert ">Youssef</option>" in html


def test_the_summary_shows_the_plan_limit_when_a_period_has_one(client) -> None:
    _services(client).quota_periods.add(start_date="2020-01-01", limit_bytes=500 * GB)
    _record_wan(client, **{"Linux-G15": 100 * GB})
    html = client.get("/ui/quota-summary").text
    assert "Plan limit" in html and "Of plan" in html


def test_with_no_period_configured_the_tab_still_works(client) -> None:
    """A household that has not set a reset date still deserves a working tab."""
    _record_wan(client, **{"Linux-G15": 10 * GB})
    assert client.get("/quota").status_code == 200
    assert "Youssef" in client.get("/ui/quota-buckets").text


# --- reassignment through the UI ----------------------------------------------------------


def test_reassigning_a_download_moves_it_between_quotas(client) -> None:
    """The Deer Hunter case: pulled on the server, counts to the household."""
    _record_wan(client, **{"Linux-G15": 100 * GB})
    row = _services(client).ledger.add_manual(
        name="The Deer Hunter", kind="movie", downloaded_bytes=3 * GB, uploaded_bytes=0
    )
    client.post(f"/quota/attribute/{row}", data={"device": "", "quota": "household"})

    moves = client.get("/ui/quota-reassignments").text
    assert "The Deer Hunter" in moves and "Linux-G15" in moves
    result = _services(client).attribution(None, None)
    assert result.get("youssef").used == 97 * GB
    assert result.get("household").used == 3 * GB
    assert result.reconciles is True


def test_an_unknown_quota_is_dropped_rather_than_stored(client) -> None:
    """A stale form must not park bytes in a bucket that does not exist."""
    row = _services(client).ledger.add_manual(
        name="x", kind="movie", downloaded_bytes=GB, uploaded_bytes=0
    )
    client.post(f"/quota/attribute/{row}", data={"device": "", "quota": "ghost"})
    assert _services(client).ledger.get(row).quota is None


def test_reassigning_does_not_freeze_the_row_against_qbittorrent(client) -> None:
    """Attribution says whose bytes these were, not how many; ``edited`` is about the latter."""
    ledger = _services(client).ledger
    ledger.record_auto(
        infohash="abc", name="t", kind="movie", app="radarr",
        downloaded_bytes=GB, uploaded_bytes=0,
    )
    row = ledger.list()[0]
    client.post(f"/quota/attribute/{row.id}", data={"device": "", "quota": "household"})
    assert ledger.get(row.id).edited == 0


# --- the config write-back ----------------------------------------------------------------


def test_assigning_a_device_writes_config_and_takes_effect(client, cfg_path: Path) -> None:
    _record_wan(client, **{"Xiaomi-TV-Box": 5 * GB})
    client.post("/quota/assign", data={"device": "Xiaomi-TV-Box", "person": "logain"})

    written = yaml.safe_load(cfg_path.read_text())
    assert written["quotas"]["devices"]["Xiaomi-TV-Box"] == "logain"
    # hot-reloaded, so the attribution moves without a restart
    assert _services(client).attribution(None, None).get("logain").used == 5 * GB


def test_clearing_an_owner_removes_the_entry(client, cfg_path: Path) -> None:
    client.post("/quota/assign", data={"device": "PC-114", "person": ""})
    assert "PC-114" not in yaml.safe_load(cfg_path.read_text())["quotas"]["devices"]


def test_an_unknown_person_is_never_written(client, cfg_path: Path) -> None:
    """Writing it would make the file fail its own validation and lock the Config tab out."""
    before = cfg_path.read_text()
    client.post("/quota/assign", data={"device": "Xiaomi-TV-Box", "person": "nobody"})
    assert cfg_path.read_text() == before


def test_the_write_keeps_the_rest_of_the_file(client, cfg_path: Path) -> None:
    client.post("/quota/assign", data={"device": "Xiaomi-TV-Box", "person": "logain"})
    data = yaml.safe_load(cfg_path.read_text())
    assert data["webhook_secret"] == "topsecret"
    assert data["quotas"]["server_device"] == "Linux-G15"
    assert set(data["quotas"]["people"]) == {"youssef", "logain"}


# --- the historical backfill --------------------------------------------------------------


def test_historical_game_rows_are_attributed_once(tmp_path, cfg_path, monkeypatch) -> None:
    """Guarded by a meta key, so clearing a device by hand is not undone by the next restart."""
    from mediacore.db import open_db
    from orchestrator.ledger import Ledger
    from orchestrator.services import _GAMES_BACKFILL_KEY, build_services

    state = tmp_path / "state"
    led = Ledger(open_db(state))
    led.add_manual(name="PEAK", kind="game", downloaded_bytes=GB, uploaded_bytes=0)
    led.add_manual(name="Her", kind="movie", downloaded_bytes=GB, uploaded_bytes=0)

    services = build_services(cfg_path)
    rows = {r.name: r.device for r in services.ledger.list()}
    assert rows["PEAK"] == "PC-114"
    assert rows["Her"] is None  # NULL means "derive it" -> the server device
    assert services.queue.get_meta(_GAMES_BACKFILL_KEY) == "PC-114"

    services.ledger.set_attribution(
        next(r.id for r in services.ledger.list() if r.name == "PEAK"), device=None, quota=None
    )
    build_services(cfg_path)  # a restart must not undo that
    assert {r.name: r.device for r in services.ledger.list()}["PEAK"] is None


# --- quota periods start at a time, not just a date ---------------------------------------


def test_a_period_can_start_partway_through_a_day(client) -> None:
    """A quota renewed at midday leaves that day's downloads split between two periods."""
    services = _services(client)
    pid = services.quota_periods.add(
        start_date="2026-09-10", start_time="14:30", limit_bytes=500 * GB
    )
    period = services.quota_periods.latest()
    assert period.id == pid and period.mid_day is True
    assert period.starts_at == "2026-09-10T14:30:00+00:00"


def test_an_existing_period_keeps_starting_at_midnight(client) -> None:
    """The column's DEFAULT is what stops a migration changing every period's meaning."""
    services = _services(client)
    services.quota_periods.add(start_date="2026-09-10")
    period = services.quota_periods.latest()
    assert period.start_time == "00:00" and period.mid_day is False
    assert period.starts_at == "2026-09-10T00:00:00+00:00"


def test_the_ledger_window_honours_the_period_time(client) -> None:
    """A download before the renewal belongs to the previous period, not this one."""
    ledger = _services(client).ledger
    ledger.add_manual(name="before", kind="movie", downloaded_bytes=GB, uploaded_bytes=0,
                      completed_at="2026-09-10T09:00:00+00:00")
    ledger.add_manual(name="after", kind="movie", downloaded_bytes=2 * GB, uploaded_bytes=0,
                      completed_at="2026-09-10T18:00:00+00:00")
    _services(client).quota_periods.add(start_date="2026-09-10", start_time="14:30")

    html = client.get("/ui/ledger").text
    assert "after" in html and "before" not in html


def test_a_mid_day_period_says_the_wan_day_cannot_be_split(client) -> None:
    """wan_usage stores one row per device per DAY, so the start day sits in both periods.

    Saying so beats implying a precision the store does not have — the same rail as the
    Bandwidth tab's "tracked as downloads" tile.
    """
    _record_wan(client, **{"Linux-G15": 10 * GB})
    _services(client).quota_periods.add(start_date="2026-09-10", start_time="14:30")
    html = client.get("/ui/quota-buckets").text
    assert "one row per device per" in html and "14:30" in html


def test_a_midnight_period_says_nothing_about_split_days(client) -> None:
    _record_wan(client, **{"Linux-G15": 10 * GB})
    _services(client).quota_periods.add(start_date="2026-09-10")
    assert "one row per device per" not in client.get("/ui/quota-buckets").text


def test_the_period_form_round_trips_a_time(client) -> None:
    client.post("/quota-periods/add",
                data={"start_date": "2026-09-10", "start_time": "14:30", "limit": "500 GB"},
                follow_redirects=False)
    period = _services(client).quota_periods.latest()
    assert (period.start_date, period.start_time) == ("2026-09-10", "14:30")
    assert "14:30" in client.get("/ui/quota-periods").text


def test_a_nonsense_time_falls_back_to_midnight_rather_than_500ing(client) -> None:
    client.post("/quota-periods/add",
                data={"start_date": "2026-09-10", "start_time": "99:99"},
                follow_redirects=False)
    assert _services(client).quota_periods.latest().start_time == "00:00"


def test_a_ledger_entry_takes_a_time_of_day(client) -> None:
    client.post("/downloads/add",
                data={"name": "midday", "kind": "movie", "downloaded": "1 GB",
                      "completed_at": "2026-09-10T14:30"},
                follow_redirects=False)
    row = next(r for r in _services(client).ledger.list() if r.name == "midday")
    assert row.completed_at == "2026-09-10T14:30:00+00:00"


def test_a_bare_date_still_parses_as_midnight(client) -> None:
    """Every row stored before the field became a datetime must keep working."""
    client.post("/downloads/add",
                data={"name": "dateonly", "kind": "movie", "downloaded": "1 GB",
                      "completed_at": "2026-09-10"},
                follow_redirects=False)
    row = next(r for r in _services(client).ledger.list() if r.name == "dateonly")
    assert row.completed_at == "2026-09-10T00:00:00+00:00"
