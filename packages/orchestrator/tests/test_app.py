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
monitoring:
  disks:
    - {{ path: {tmp_path}, label: TMP }}
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


def test_monitoring_page(client) -> None:
    r = client.get("/monitoring")
    assert r.status_code == 200
    assert "Monitoring" in r.text


def test_mon_partials_render(client) -> None:
    assert client.get("/ui/mon-summary").status_code == 200
    disks = client.get("/ui/mon-disks")
    assert disks.status_code == 200 and "TMP" in disks.text
    alerts = client.get("/ui/mon-alerts")
    assert alerts.status_code == 200 and "all clear" in alerts.text
    containers = client.get("/ui/mon-containers")
    # docker monitoring is off in the test config → the partial shows the enable hint
    assert containers.status_code == 200 and "monitoring.docker" in containers.text
    system = client.get("/ui/mon-system")
    # system monitoring is off in the test config → the partial shows the enable hint
    assert system.status_code == 200 and "monitoring.system" in system.text
    smart = client.get("/ui/mon-smart")
    # SMART monitoring is off in the test config → the partial shows the enable hint
    assert smart.status_code == 200 and "monitoring.smart" in smart.text
    events = client.get("/ui/mon-events")
    assert events.status_code == 200 and "No alert events" in events.text


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


def test_retry_dead_redirects_to_jobs(client) -> None:
    r = client.post("/jobs/retry-dead", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/jobs"


def test_job_actions_redirect_to_jobs(client) -> None:
    # worker + rerun actions land back on the Jobs tab, not the overview
    for path in ("/worker/pause", "/worker/resume", "/jobs/clear-dead"):
        r = client.post(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/jobs"


def test_overview_dashboard(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    # the overview composes widgets from the other tabs via data-src
    for src in ("/ui/summary", "/ui/mon-system", "/ui/mon-disks", "/ui/mon-alerts",
                "/ui/mon-containers-summary", "/ui/worker?readonly=1", "/ui/actions?limit=10"):
        assert src in r.text


def test_jobs_page(client) -> None:
    r = client.get("/jobs")
    assert r.status_code == 200
    # the job queue moved here: search box + active/failed panels
    assert 'id="search"' in r.text and "/ui/active" in r.text and "/ui/failed" in r.text


def test_worker_readonly_hides_controls(client) -> None:
    # overview worker widget is read-only: status shown, no pause/resume form
    ro = client.get("/ui/worker", params={"readonly": "1"}).text
    assert "Running" in ro and "/worker/pause" not in ro
    # the Jobs-tab worker widget keeps the controls
    assert "/worker/pause" in client.get("/ui/worker").text


def test_actions_limit(client) -> None:
    assert client.get("/ui/actions", params={"limit": "5"}).status_code == 200


def test_containers_summary_widget(client) -> None:
    # docker off in the test config → the compact widget shows the enable hint
    r = client.get("/ui/mon-containers-summary")
    assert r.status_code == 200 and "monitoring.docker" in r.text


def test_config_page(client) -> None:
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


# --- library tab -------------------------------------------------------------------------
def _index_row(
    services,
    path: str,
    *,
    needs_work: int = 1,
    app: str = "radarr",
    delta_bytes: int = 0,
    delta_known: int = 1,
):
    from orchestrator.library import LibraryRow

    services.library.upsert(
        LibraryRow(
            path=path,
            app=app,
            root="/lib",
            name=Path(path).name,
            size_bytes=1000,
            mtime=1.0,
            needs_work=needs_work,
            reasons="subs" if needs_work else "",
            summary="dropped 1 foreign sub(s) [fre]" if needs_work else "nothing to do",
            drop_subs=needs_work,
            delta_bytes=delta_bytes,
            delta_known=delta_known,
            scanned_at="2026-01-01T00:00:00.000000+00:00",
        )
    )


def test_library_page_and_partials_render(client) -> None:
    services = client.app.state.services
    _index_row(services, "/lib/a.mkv")

    page = client.get("/library")
    assert page.status_code == 200
    assert "Scan library" in page.text

    table = client.get("/ui/library", params={"status": "needs"})
    assert table.status_code == 200
    assert "a.mkv" in table.text

    summary = client.get("/ui/library-summary")
    assert summary.status_code == 200
    assert "Need work" in summary.text


def test_library_scan_queues_one_job_at_a_time(client) -> None:
    from orchestrator.dispatch import LIBRARY_SCAN

    services = client.app.state.services
    client.post("/library/scan", data={"root": "", "full": ""}, follow_redirects=False)
    client.post("/library/scan", data={"root": "", "full": ""}, follow_redirects=False)
    scans = [j for j in services.queue.list(limit=50) if j.kind == LIBRARY_SCAN]
    assert len(scans) == 1
    assert scans[0].payload == {"root": None, "full": False}


def test_library_enqueue_ignores_paths_that_are_not_indexed(client) -> None:
    services = client.app.state.services
    _index_row(services, "/lib/a.mkv")

    resp = client.post(
        "/library/enqueue",
        data={"paths": ["/lib/a.mkv", "/etc/passwd"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    labels = [j.label for j in services.queue.list(limit=50)]
    assert labels == ["re-run a.mkv"]
    assert services.library.get("/lib/a.mkv").enqueued_at is not None


def test_library_enqueue_select_all_uses_the_filters_and_the_cap(client) -> None:
    services = client.app.state.services
    services.config.library.max_enqueue = 2
    for i in range(4):
        _index_row(services, f"/lib/movie{i}.mkv")
    _index_row(services, "/lib/clean.mkv", needs_work=0)

    client.post(
        "/library/enqueue",
        data={"select_all": "1", "status": "needs", "q": "movie"},
        follow_redirects=False,
    )
    jobs = services.queue.list(limit=50)
    assert len(jobs) == 2  # capped
    assert all("movie" in (j.label or "") for j in jobs)


def test_library_queued_badge_clears_when_the_job_is_cancelled(client) -> None:
    """End to end: queue a file, cancel the job, and the Library tab must stop counting it."""
    services = client.app.state.services
    _index_row(services, "/lib/a.mkv")
    client.post("/library/enqueue", data={"paths": ["/lib/a.mkv"]}, follow_redirects=False)
    assert 'tile-label">Queued<' in client.get("/ui/library-summary").text

    job = services.queue.list(limit=10)[0]
    services.queue.cancel(job.id)
    assert 'tile-label">Queued<' not in client.get("/ui/library-summary").text
    assert "needs work" in client.get("/ui/library", params={"status": "needs"}).text


def test_library_table_publishes_the_totals_the_confirmation_quotes(client) -> None:
    """The bulk-enqueue confirmation has to quote what will really be queued. The numbers come
    off the rendered wrapper, and must be scoped to actionable rows under the *current* filters —
    a clean file is a row in the table but never a file the enqueue touches."""
    services = client.app.state.services
    _index_row(services, "/lib/movie0.mkv", delta_bytes=-500)
    _index_row(services, "/lib/movie1.mkv", delta_bytes=200, delta_known=0)
    _index_row(services, "/lib/clean.mkv", needs_work=0, delta_bytes=-9999)

    body = client.get("/ui/library").text
    assert 'data-matching-count="2"' in body  # clean.mkv excluded
    assert 'data-matching-delta="-300"' in body  # -500 + 200, clean.mkv's -9999 excluded
    assert 'data-matching-unknown="1"' in body

    # ...and it follows the filters, so "select all matching" can't quote the unfiltered library
    filtered = client.get("/ui/library", params={"q": "movie0"}).text
    assert 'data-matching-count="1"' in filtered
    assert 'data-matching-delta="-500"' in filtered


def test_library_rows_carry_their_delta_for_a_cross_page_selection(client) -> None:
    """The selection spans pages, so each checkbox carries its own delta — page 2 can't re-read
    page 1's rows."""
    services = client.app.state.services
    _index_row(services, "/lib/a.mkv", delta_bytes=-1234, delta_known=0)

    body = client.get("/ui/library").text
    assert 'data-delta="-1234"' in body
    assert 'data-known="0"' in body


def test_library_summary_shows_a_running_scans_progress(client) -> None:
    """A scan runs for tens of minutes; the tab that starts it must show how far along it is
    rather than sending the reader to /jobs."""
    from orchestrator.dispatch import LIBRARY_SCAN

    services = client.app.state.services
    idle = client.get("/ui/library-summary").text
    assert "jobbar-fill" not in idle
    assert "Last scan" in idle

    services.queue.enqueue(LIBRARY_SCAN, {"root": None, "full": False}, label="library scan")
    job = services.queue.claim_next()
    assert job is not None
    services.queue.set_progress(job.id, 42.0, "900/2208 · 137 need work")

    body = client.get("/ui/library-summary").text
    assert "42%" in body
    assert "900/2208 · 137 need work" in body
    assert 'style="width: 42%"' in body
    assert f'/jobs/{job.id}' in body


def test_library_summary_distinguishes_a_queued_scan_from_a_running_one(client) -> None:
    """A scan waiting behind another job has no progress yet — saying "scanning…" would be a
    lie, and a 0% bar would read as a stall."""
    from orchestrator.dispatch import LIBRARY_SCAN

    services = client.app.state.services
    services.queue.enqueue(LIBRARY_SCAN, {"root": None, "full": False}, label="library scan")

    body = client.get("/ui/library-summary").text
    assert "queued" in body
    assert "waiting for the worker" in body


def test_the_job_lists_are_scrollable(client) -> None:
    """A 95-job queue made the card thousands of pixels tall. The scroller has to live INSIDE the
    partial: on the panel element itself, app.js can't find it to restore scrollTop, and a 2s
    poll would yank the reader back to the top."""
    services = client.app.state.services
    for i in range(3):
        services.queue.enqueue("rerun", {"app": "sonarr", "payload": {}}, label=f"job {i}")
    for path in ("/ui/active", "/ui/failed"):
        html = client.get(path).text
        if "table" not in html:
            continue
        assert '<div class="scroll-y">' in html, f"{path} is not scrollable"
        assert html.index('<div class="scroll-y">') < html.index("<table"), "scroller must wrap"


# --- monitoring routes read snapshots, never the source ------------------------------------
def test_mon_summary_reads_the_snapshot_and_never_the_docker_socket(client) -> None:
    """This partial polls every 5s on two pages. It used to call services.docker.containers()
    live, so every poll from every viewer was a Docker socket round-trip — exactly what the
    worker's snapshot exists to avoid."""
    from mediacore.docker_status import ContainerStatus
    from orchestrator.monitor import SourceSnapshot

    services = client.app.state.services

    class Exploding:
        name = "docker"

        @property
        def enabled(self) -> bool:
            return True

        @property
        def interval_s(self) -> int:
            return 30

        def collect(self):
            raise AssertionError("a web route must not collect from a source")

    services.sources = [Exploding()]
    services.snapshots["docker"] = SourceSnapshot(
        data=[
            ContainerStatus(name="radarr", state="running", status="Up", health=None, up=True),
            ContainerStatus(name="sonarr", state="exited", status="Exited", health=None, up=False),
        ],
        ok=True,
    )

    body = client.get("/ui/mon-summary").text
    assert "Containers up" in body and "1/2" in body


def test_an_unreachable_source_says_so_instead_of_showing_stale_rows(client) -> None:
    """ok=False must reach the UI as its own state. Rendering the last good snapshot would
    present numbers from an unknown time ago as if they were current."""
    from orchestrator.monitor import SourceSnapshot

    services = client.app.state.services

    class On:
        name = "smart"
        enabled = True
        interval_s = 900

        def collect(self):  # pragma: no cover - never called by a route
            raise AssertionError

    services.sources = [On()]
    services.snapshots["smart"] = SourceSnapshot(data=None, ok=False)
    assert "Scrutiny unreachable" in client.get("/ui/mon-smart").text

    services.snapshots.pop("smart")
    assert "Collecting SMART data" in client.get("/ui/mon-smart").text


def test_a_source_switched_off_says_off_not_collecting(client) -> None:
    services = client.app.state.services
    services.sources = []  # nothing configured at all
    assert "SMART monitoring is off" in client.get("/ui/mon-smart").text
    assert "Container monitoring is off" in client.get("/ui/mon-containers").text
    assert "System monitoring is off" in client.get("/ui/mon-system").text


def test_library_sorting_moved_from_chips_into_the_table_headers(client) -> None:
    """The toolbar was 19 controls. Sorting belongs where people look for it, and the headers
    cover all four sorts the index supports, so the chip group is gone."""
    services = client.app.state.services
    _index_row(services, "/lib/a.mkv")

    page = client.get("/library").text
    assert 'id="lib-sort"' not in page  # the chip group
    assert "data-sortable" in page  # the panel now asks for a sort param

    body = client.get("/ui/library").text
    for key in ("name", "size", "impact", "scanned"):
        assert f'data-sort="{key}"' in body
    # the active column is marked, so the header row says which way the table is ordered
    assert 'c-delta sortable sorted' in body  # impact is the default
    assert 'c-file sortable"' in body

    by_name = client.get("/ui/library", params={"sort": "name"}).text
    assert 'c-file sortable sorted' in by_name
    assert 'c-delta sortable"' in by_name


def test_library_rows_show_when_they_were_last_scanned(client) -> None:
    """What the `scanned` sort orders by has to be visible, or the sort is a mystery."""
    services = client.app.state.services
    _index_row(services, "/lib/a.mkv")
    assert "2026-01-01" in client.get("/ui/library").text


def test_the_scan_form_opens_itself_on_a_first_run(client) -> None:
    """An empty index has exactly one useful action; hiding it behind a toggle above an empty
    table is the worst possible first impression."""
    services = client.app.state.services
    empty = client.get("/library").text
    assert 'id="scan-panel" hidden' not in empty
    assert 'aria-expanded="true"' in empty

    _index_row(services, "/lib/a.mkv")
    indexed = client.get("/library").text
    assert 'id="scan-panel" hidden' in indexed
    assert 'aria-expanded="false"' in indexed


def test_chip_groups_are_labelled_for_assistive_tech(client) -> None:
    """They are bare <button>s in a <div>; without a role and a label they are an unexplained
    run of buttons."""
    for path in ("/library", "/jobs", "/downloads"):
        body = client.get(path).text
        assert 'role="group"' in body
        assert "aria-label=" in body


def test_the_nav_groups_its_pairs_and_names_the_ledger_bandwidth(client) -> None:
    body = client.get("/").text
    assert "nav-group" in body
    assert ">Bandwidth<" in body  # not "Downloads": it is a bandwidth ledger, not a client
    assert ">Downloads<" not in body


def test_the_config_page_offers_a_generated_settings_reference(client) -> None:
    page = client.get("/config").text
    assert "Settings reference" in page
    assert 'data-src="/ui/config-schema"' in page

    body = client.get("/ui/config-schema").text
    assert "pipeline" in body and "worker" in body
    assert "max_attempts" in body and "steps" in body
    assert "dead-lettered" in body  # the #: comment became the field's note


# --- WAN panels ------------------------------------------------------------------------------


def _enable_wan(client):
    """Switch the router source on for a test, with a stub client so nothing dials out."""
    from mediacore.router import DeviceStats
    from orchestrator.monitor import build_sources

    services = client.app.state.services
    services.config.monitoring.router.enabled = True
    services.config.monitoring.router.password = "pw"
    services.config.monitoring.router.manage_settings = False
    services.sources = build_sources(services.config.monitoring, wan=services._wan_deps())
    src = services.source("wan")
    src.client.devices = lambda: [  # type: ignore[union-attr]
        DeviceStats("AA:BB", "TV", "wired", 1000, 100, curr_rx=4096, curr_tx=512),
    ]
    return services, src


def test_a_paused_panel_never_renders_stale_rates_as_live(client) -> None:
    """Regression: the Monitoring panel read the pause flag out of the *snapshot*, which only
    learns about a pause on the next collection — so between pressing Pause and the next poll it
    kept rendering the last rates as though they were live. Exactly the failure the ok flag and
    the source-state preamble exist to prevent."""
    services, src = _enable_wan(client)
    from orchestrator.monitor import SourceSnapshot

    src.collect()  # cold start; populates a snapshot with real-looking rates
    _s, _e, data = src.collect()
    services.snapshots["wan"] = SourceSnapshot(data=data, ok=True)
    assert "Down now" in client.get("/ui/mon-wan").text

    client.post("/wan/pause", data={"minutes": 15}, follow_redirects=False)
    body = client.get("/ui/mon-wan").text
    assert "Polling paused" in body
    assert "Down now" not in body  # the stale rate must be gone, not merely relabelled


def test_pause_minutes_are_clamped(client) -> None:
    """A typo must not blind the household's usage for a week."""
    from orchestrator.monitor.wan import PAUSE_KEY, paused_until

    services, _src = _enable_wan(client)
    client.post("/wan/pause", data={"minutes": 99999}, follow_redirects=False)
    until = paused_until(services.queue.get_meta)
    assert until is not None
    import datetime as _dt

    remaining = (until - _dt.datetime.now(_dt.UTC)).total_seconds()
    assert remaining <= 240 * 60 + 5
    client.post("/wan/resume", follow_redirects=False)
    assert services.queue.get_meta(PAUSE_KEY) is None


def test_wan_panels_are_absent_when_the_source_is_off(client) -> None:
    """No half-rendered panel on a stack that has no router configured."""
    assert client.get("/ui/wan-summary").text.strip() == ""
    assert client.get("/ui/wan-pause").text.strip() == ""
    assert "is off" in client.get("/ui/wan-devices").text


def test_wan_summary_reports_the_ledger_share(client) -> None:
    """The gap between household WAN and the download ledger is the point of the panel."""
    _services, src = _enable_wan(client)
    src.collect()
    from mediacore.router import DeviceStats

    src.client.devices = lambda: [  # type: ignore[union-attr]
        DeviceStats("AA:BB", "TV", "wired", 3000, 100, curr_rx=0, curr_tx=0)
    ]
    src.collect()
    body = client.get("/ui/wan-summary?range=all").text
    assert "Household WAN" in body
    assert "Tracked as downloads" in body
