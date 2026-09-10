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


def test_a_mid_day_renewal_gives_the_whole_start_day_to_the_previous_period(client) -> None:
    """The boundary-day rule, and the reason for it.

    wan_usage stores one row per device per DAY and nothing in this database records per-device
    usage more finely, so a day that belongs to two periods cannot be divided. The whole of it
    goes to the previous period: that under-counts the new one by whatever ran between the
    renewal and midnight, which beats crediting it with a day of traffic that mostly predates it.
    """
    ledger = _services(client).ledger
    for name, when in [("before renewal", "2026-09-10T09:00:00+00:00"),
                       ("after renewal", "2026-09-10T18:00:00+00:00"),
                       ("next day", "2026-09-11T02:00:00+00:00")]:
        ledger.add_manual(name=name, kind="movie", downloaded_bytes=GB, uploaded_bytes=0,
                          completed_at=when)
    _services(client).quota_periods.add(start_date="2026-09-10", start_time="14:30")

    html = client.get("/ui/ledger").text
    assert "next day" in html
    assert "after renewal" not in html   # the start day is the previous period's, in full
    assert "before renewal" not in html


def test_the_effective_start_is_the_next_midnight_when_renewed_mid_day(client) -> None:
    services = _services(client)
    services.quota_periods.add(start_date="2026-09-10", start_time="16:37")
    period = services.quota_periods.latest()
    assert period.starts_at == "2026-09-10T16:37:00+00:00"      # what was actually set
    assert period.effective_start == "2026-09-11T00:00:00+00:00"  # where accounting begins
    assert period.boundary_day == "2026-09-10"


def test_a_midnight_period_needs_no_boundary_day(client) -> None:
    services = _services(client)
    services.quota_periods.add(start_date="2026-09-10")
    period = services.quota_periods.latest()
    assert period.effective_start == period.starts_at
    assert period.boundary_day is None


def test_a_mid_day_period_explains_where_the_start_day_went(client) -> None:
    _record_wan(client, **{"Linux-G15": 10 * GB})
    _services(client).quota_periods.add(start_date="2026-09-10", start_time="14:30")
    html = client.get("/ui/quota-buckets").text
    assert "counts entirely toward the previous period" in html
    assert "14:30" in html and "2026-09-11" in html


def test_a_midnight_period_says_nothing_about_split_days(client) -> None:
    _record_wan(client, **{"Linux-G15": 10 * GB})
    _services(client).quota_periods.add(start_date="2026-09-10")
    assert "counts entirely toward the previous period" not in client.get("/ui/quota-buckets").text


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


def test_the_summary_compares_against_the_period_being_viewed(client) -> None:
    """A limit belongs to its own period.

    Showing the current period's cap beside the previous period's usage reported 507 GB as 101%
    of a 500 GB plan that period was never held to — its own limit was 900 GB.
    """
    periods = _services(client).quota_periods
    periods.add(start_date="2026-08-22", limit_bytes=900 * GB, note="August")
    periods.add(start_date="2026-09-10", limit_bytes=500 * GB, note="September")
    _record_wan(client, **{"Linux-G15": 600 * GB})

    assert "900.00 GB" in client.get("/ui/quota-summary?period=previous").text
    assert "500.00 GB" in client.get("/ui/quota-summary?period=current").text


def test_all_time_is_held_to_no_single_limit(client) -> None:
    """It spans every period, so no one period's cap applies to it."""
    _services(client).quota_periods.add(start_date="2026-09-10", limit_bytes=500 * GB)
    _record_wan(client, **{"Linux-G15": 10 * GB})
    html = client.get("/ui/quota-summary?period=all").text
    assert "Plan limit" not in html and "Of plan" not in html


# --- the start-day snapshot ----------------------------------------------------------------


def _wan_add(client, host: str, extra: int) -> None:
    """Add traffic to a device that has already been seen, so the day's row accumulates."""
    _record_wan(client, **{host: extra})


def test_renew_now_divides_today_exactly(client) -> None:
    """The mechanism, end to end.

    wan_usage accumulates into one row per device per day, so at the renewal instant that row is
    exactly the traffic that predates the period. Snapshotting it there is what makes a mid-day
    renewal exact instead of costing the whole day.
    """
    _record_wan(client, **{"Linux-G15": 10 * GB})          # before the renewal
    client.post("/quota/renew", data={"limit": "500 GB"}, follow_redirects=False)
    _wan_add(client, "Linux-G15", 4 * GB)                   # after it

    services = _services(client)
    period = services.quota_periods.latest()
    assert period.split_captured == 1 and period.exact is True
    assert period.effective_start == period.starts_at      # no next-midnight fallback

    result = services.attribution(
        period.effective_start, None, subtract=services.period_subtraction(period, side="after")
    )
    assert result.household == 4 * GB


def test_the_two_halves_of_a_divided_day_add_up(client) -> None:
    """Dividing a day must not create or destroy bytes, same invariant as everything else here."""
    _record_wan(client, **{"Linux-G15": 10 * GB, "PC-114": 3 * GB})
    client.post("/quota/renew", data={}, follow_redirects=False)
    _wan_add(client, "Linux-G15", 4 * GB)

    services = _services(client)
    period = services.quota_periods.latest()
    after = services.attribution(
        period.effective_start, None, subtract=services.period_subtraction(period, side="after")
    ).household
    before = services.attribution(
        None, period.effective_start,
        subtract=services.period_subtraction(period, side="before"),
    ).household
    assert before + after == 17 * GB
    assert (before, after) == (13 * GB, 4 * GB)


def test_a_period_typed_after_the_fact_gets_no_snapshot(client) -> None:
    """The instant cannot be recovered, so the honest fallback stands."""
    _record_wan(client, **{"Linux-G15": 10 * GB})
    _services(client).quota_periods.add(start_date="2020-01-05", start_time="16:37")
    period = _services(client).quota_periods.latest()
    assert _services(client).capture_period_split(period.id) is False
    assert period.split_captured == 0 and period.exact is False


def test_moving_a_periods_start_discards_its_snapshot(client) -> None:
    """A snapshot describes one instant; move the start and it describes the wrong one.

    A precise answer to the wrong question is worse than the honest fallback.
    """
    _record_wan(client, **{"Linux-G15": 10 * GB})
    client.post("/quota/renew", data={}, follow_redirects=False)
    services = _services(client)
    period = services.quota_periods.latest()
    assert period.split_captured == 1

    services.quota_periods.update(
        period.id, start_date=period.start_date, start_time="03:00"
    )
    moved = services.quota_periods.get(period.id)
    assert moved.split_captured == 0
    assert services.quota_periods.split(period.id) == {}


def test_editing_only_the_limit_keeps_the_snapshot(client) -> None:
    """The start did not move, so the snapshot still describes it."""
    _record_wan(client, **{"Linux-G15": 10 * GB})
    client.post("/quota/renew", data={}, follow_redirects=False)
    services = _services(client)
    period = services.quota_periods.latest()
    services.quota_periods.update(
        period.id, start_date=period.start_date, start_time=period.start_time,
        limit_bytes=900 * GB,
    )
    assert services.quota_periods.get(period.id).split_captured == 1


def test_a_snapshot_of_nothing_still_counts_as_captured(client) -> None:
    """"No device had used anything yet today" is a real answer, not a missing one."""
    client.post("/quota/renew", data={}, follow_redirects=False)
    services = _services(client)
    period = services.quota_periods.latest()
    assert period.split_captured == 1 and services.quota_periods.split(period.id) == {}


def test_deleting_a_period_takes_its_snapshot_with_it(client) -> None:
    _record_wan(client, **{"Linux-G15": 10 * GB})
    client.post("/quota/renew", data={}, follow_redirects=False)
    services = _services(client)
    pid = services.quota_periods.latest().id
    services.quota_periods.delete(pid)
    assert services.quota_periods.split(pid) == {}


def test_the_tab_says_the_day_was_divided_exactly(client) -> None:
    _record_wan(client, **{"Linux-G15": 10 * GB})
    client.post("/quota/renew", data={}, follow_redirects=False)
    _wan_add(client, "Linux-G15", 4 * GB)
    html = client.get("/ui/quota-buckets").text
    assert "divided exactly" in html
    assert "counts entirely toward the previous period" not in html
