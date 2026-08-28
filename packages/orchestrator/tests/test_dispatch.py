from __future__ import annotations

import json
import os
import types
from pathlib import Path

import pytest
from conftest import build_test_services, mk, probe_of, radarr_config
from mediacore.audit import FAIL, OK, SKIP
from orchestrator.dispatch import Dispatcher, DispatchError
from orchestrator.runner import PruneResult


def _touch(tmp_path: Path) -> str:
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x")
    return str(f)


def _radarr_payload(path: str) -> dict:
    return {
        "app": "radarr",
        "payload": {
            "eventType": "Download",
            "movie": {"id": 5, "title": "Mov", "year": 2021},
            "movieFile": {"path": path},
        },
    }


def _dirty_probe(path: str):
    return probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="opus", channels=6),  # -> re-encode
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),  # -> drop
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),  # keep
        path=path,
    )


def test_prune_pipeline_runs_and_rescans(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait",
        lambda self, item_id, **kw: calls.append(item_id),
    )
    cfg = radarr_config(enable_seasonal_shift=False)
    services = build_test_services(tmp_path, cfg)
    path = _touch(tmp_path)

    argv_seen: list = []

    def fake_runner(argv, **k):
        argv_seen.append(argv)
        return PruneResult(0, "ok", "")

    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p), prune_runner=fake_runner)
    outcome = disp.dispatch("job1", _radarr_payload(path))

    results = {s.tool: s.result for s in outcome.steps}
    assert results["quality-check"] == OK
    assert results["mkvprune"] == OK
    assert calls == [5]  # rescan called with movie id
    # argv keeps all audio, drops foreign sub (id 2) -> keep only id 3; --progress-json injected
    assert argv_seen[0] == ["mkvprune", "--progress-json", "-y", "--audio", "all", "--subs", "3", path]

    rows = services.audit.for_job("job1")
    assert any(r["tool"] == "mkvprune" and r["result"] == OK for r in rows)


def test_prune_skips_when_clean(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait",
        lambda self, item_id, **kw: calls.append(item_id),
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)
    clean = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="dts", channels=6),
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),
        path=path,
    )
    disp = Dispatcher(services, probe_fn=lambda p: clean, prune_runner=lambda a, **k: PruneResult(0, "", ""))
    outcome = disp.dispatch("j", _radarr_payload(path))
    prune = next(s for s in outcome.steps if s.tool == "mkvprune")
    assert prune.result == SKIP
    assert calls == []  # no rescan when nothing changed


def test_remux_mp4_to_mkv(tmp_path, monkeypatch):
    """A clean .mp4 under remux_to_mkv is converted: prune runs, the source becomes .mkv, the
    job's final path follows, and the arr is rescanned."""
    calls: list = []
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait",
        lambda self, item_id, **kw: calls.append(item_id),
    )
    cfg = radarr_config(enable_seasonal_shift=False)
    cfg.policy.remux_to_mkv = True
    services = build_test_services(tmp_path, cfg)
    mp4 = tmp_path / "movie.mp4"
    mp4.write_bytes(b"x")
    path = str(mp4)
    clean = probe_of(  # nothing to prune, but .mp4 → remux forced
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="dts", channels=6),
        mk(2, "subtitle", codec_name="subrip", language="eng"),
        path=path,
    )

    def fake_runner(argv, **k):  # simulate mkvprune's cross-format replace
        out = os.path.splitext(argv[-1])[0] + ".mkv"
        with open(out, "w") as f:
            f.write("mkv")
        os.remove(argv[-1])  # source .mp4 removed after verified write
        return PruneResult(0, f"Done: {out}", "")

    disp = Dispatcher(services, probe_fn=lambda p: clean, prune_runner=fake_runner)
    outcome = disp.dispatch("jremux", _radarr_payload(path))

    prune = next(s for s in outcome.steps if s.tool == "mkvprune")
    assert prune.result == OK and "remux" in prune.detail
    assert outcome.final_local_path.endswith("movie.mkv")
    assert calls == [5]  # rescanned so Radarr adopts the .mkv
    assert (tmp_path / "movie.mkv").exists() and not (tmp_path / "movie.mp4").exists()


def test_remux_disabled_leaves_clean_mp4(tmp_path, monkeypatch):
    """With remux_to_mkv off, a clean .mp4 still skips (existing behavior)."""
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, item_id, **kw: None
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    mp4 = tmp_path / "movie.mp4"
    mp4.write_bytes(b"x")
    clean = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="dts", channels=6),
        mk(2, "subtitle", codec_name="subrip", language="eng"),
        path=str(mp4),
    )
    disp = Dispatcher(services, probe_fn=lambda p: clean, prune_runner=lambda a, **k: PruneResult(0, "", ""))
    outcome = disp.dispatch("jno", _radarr_payload(str(mp4)))
    prune = next(s for s in outcome.steps if s.tool == "mkvprune")
    assert prune.result == SKIP and mp4.exists()


def test_prune_dedup_second_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, item_id, **kw: None
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p), prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("j1", _radarr_payload(path))
    outcome2 = disp.dispatch("j2", _radarr_payload(path))
    prune = next(s for s in outcome2.steps if s.tool == "mkvprune")
    assert prune.result == SKIP and "already pruned" in prune.detail


def test_prune_failure_raises(tmp_path):
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)
    disp = Dispatcher(
        services,
        probe_fn=lambda p: _dirty_probe(p),
        prune_runner=lambda a, **k: PruneResult(3, "", "verify failed"),
    )
    with pytest.raises(DispatchError):
        disp.dispatch("j", _radarr_payload(path))


def test_probe_failure_is_nonfatal_quality_but_fatal_prune(tmp_path):
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)

    def boom(_p):
        raise RuntimeError("no ffprobe")

    disp = Dispatcher(services, probe_fn=boom, prune_runner=lambda a, **k: PruneResult(0, "", ""))
    # quality-check records FAIL (non-fatal), prune also fails to probe -> FAIL step (non-fatal here
    # since decision needs a probe; a probe error in prune is reported, not raised)
    outcome = disp.dispatch("j", _radarr_payload(path))
    qc = next(s for s in outcome.steps if s.tool == "quality-check")
    assert qc.result == FAIL


def test_seasonal_shift_uses_original_episode_numbers(tmp_path, monkeypatch):
    """Regression: the shifted file (S02E03 -> S01E28) must be re-registered with Sonarr
    under the ORIGINAL episode (2,3), not the shifted (1,28)."""
    from mediacore.arr.client import SonarrClient
    from mediacore.models import ArrInstance, CoreConfig
    from orchestrator.config import OrchestratorConfig
    from seasonal_shift.models import SeasonConfig, ShowConfig

    show_dir = tmp_path / "shows" / "MyShow"
    (show_dir / "Season 2").mkdir(parents=True)
    src = show_dir / "Season 2" / "MyShow - S02E03 - Title.mkv"
    src.write_bytes(b"x")

    cfg = OrchestratorConfig(enable_quality_check=False, enable_prune=False)
    cfg.arr = CoreConfig().arr
    cfg.arr.sonarr = ArrInstance(base_url="http://sonarr", api_key="k")
    cfg.ntfy.status_granularity = "off"  # don't reach out to a real ntfy in tests
    cfg.shows = [
        ShowConfig(
            name="MyShow",
            path=show_dir,
            seasons={2: SeasonConfig(season_offset=-1, episode_offset=25)},
        )
    ]
    services = build_test_services(tmp_path, cfg)

    calls: dict = {}

    def rec_get_episode_id(self, sid, s, e):
        calls["get_episode_id"] = (sid, s, e)
        return 555

    def rec_detect_file(self, sid, path, season, ep, **k):
        calls["detect_file"] = {"season": season, "ep": ep, "path": path, "kw": k}
        return {"c": 1}

    monkeypatch.setattr(SonarrClient, "get_episode_id", rec_get_episode_id)
    monkeypatch.setattr(SonarrClient, "get_episode_file",
                        lambda self, ep: {"quality": {"q": 1}, "releaseGroup": "RG"})
    monkeypatch.setattr(SonarrClient, "refresh", lambda self, i: {"id": 1})
    monkeypatch.setattr(SonarrClient, "wait_for_command", lambda self, cid, **k: None)
    monkeypatch.setattr(SonarrClient, "detect_file", rec_detect_file)
    monkeypatch.setattr(SonarrClient, "import_file",
                        lambda self, sid, ep, cand: calls.setdefault("import_file", (sid, ep)))

    payload = {
        "app": "sonarr",
        "payload": {
            "eventType": "Download",
            "series": {"id": 42, "title": "MyShow"},
            "episodes": [{"seasonNumber": 2, "episodeNumber": 3}],
            "episodeFile": {"path": str(src)},
        },
    }
    disp = Dispatcher(services, probe_fn=lambda p: None, prune_runner=lambda a, **k: None)
    outcome = disp.dispatch("jshift", payload)

    shift = next(s for s in outcome.steps if s.tool == "seasonal-shift")
    assert shift.result == OK, shift.error
    # original numbers used for lookup, shifted file on disk
    assert calls["get_episode_id"] == (42, 2, 3)
    assert calls["detect_file"]["season"] == 2 and calls["detect_file"]["ep"] == 555
    assert calls["detect_file"]["kw"].get("release_group") == "RG"
    assert (show_dir / "Season 1" / "MyShow - S01E28 - Title.mkv").exists()
    assert not src.exists()


class _StubQbit:
    def __init__(self, torrent):
        self._t = torrent

    def torrent_by_hash(self, h):
        return self._t


def test_ledger_capture_records_bandwidth(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, item_id, **kw: None
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    services.qbit = _StubQbit({
        "name": "Mov.2021", "downloaded": 1000, "uploaded": 200, "total_size": 900,
        "category": "Movies", "completion_on": 1700000000, "added_on": 0, "state": "up", "hash": "h",
    })
    path = _touch(tmp_path)
    payload = _radarr_payload(path)
    payload["payload"]["downloadId"] = "ABCDEF"
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p),
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("jl", payload)

    rows = services.ledger.list()
    assert len(rows) == 1
    assert rows[0].downloaded_bytes == 1000 and rows[0].uploaded_bytes == 200
    assert rows[0].kind == "movie" and rows[0].infohash == "abcdef"
    assert any(r["tool"] == "ledger" and r["result"] == "ok" for r in services.audit.for_job("jl"))


def test_ledger_capture_skips_without_downloadid(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, item_id, **kw: None
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    services.qbit = _StubQbit({"name": "x", "downloaded": 1, "uploaded": 0, "total_size": 0,
                               "category": "", "completion_on": 0, "added_on": 0, "state": "", "hash": "h"})
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p),
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("jn", _radarr_payload(_touch(tmp_path)))  # no downloadId
    assert services.ledger.list() == []


def test_prune_runs_before_seasonal_shift(tmp_path, monkeypatch):
    """Ordering fix: prune probes the stable imported path (S02E03) BEFORE seasonal-shift moves
    it, so it never no-ops on a relocated post-shift path."""
    from mediacore.arr.client import SonarrClient
    from mediacore.models import ArrInstance, CoreConfig
    from orchestrator.config import OrchestratorConfig
    from seasonal_shift.models import SeasonConfig, ShowConfig

    show_dir = tmp_path / "shows" / "MyShow"
    (show_dir / "Season 2").mkdir(parents=True)
    src = show_dir / "Season 2" / "MyShow - S02E03 - Title.mkv"
    src.write_bytes(b"x")

    cfg = OrchestratorConfig(enable_quality_check=True, enable_prune=True)
    cfg.arr = CoreConfig().arr
    cfg.arr.sonarr = ArrInstance(base_url="http://sonarr", api_key="k")
    cfg.ntfy.status_granularity = "off"
    cfg.shows = [ShowConfig(name="MyShow", path=show_dir,
                            seasons={2: SeasonConfig(season_offset=-1, episode_offset=25)})]
    services = build_test_services(tmp_path, cfg)

    monkeypatch.setattr(SonarrClient, "get_episode_id", lambda self, *a, **k: 1)
    monkeypatch.setattr(SonarrClient, "get_episode_file", lambda self, *a, **k: {})
    monkeypatch.setattr(SonarrClient, "refresh", lambda self, *a, **k: {"id": 1})
    monkeypatch.setattr(SonarrClient, "wait_for_command", lambda self, *a, **k: None)
    monkeypatch.setattr(SonarrClient, "detect_file", lambda self, *a, **k: {})
    monkeypatch.setattr(SonarrClient, "import_file", lambda self, *a, **k: {})

    pruned_paths = []
    probe_paths = []

    def fake_probe(p):
        probe_paths.append(p)
        return _dirty_probe(p)

    def fake_runner(argv, **k):
        pruned_paths.append(argv[-1])
        return PruneResult(0, "", "")

    payload = {"app": "sonarr", "payload": {
        "eventType": "Download", "series": {"id": 42, "title": "MyShow"},
        "episodes": [{"seasonNumber": 2, "episodeNumber": 3}],
        "episodeFile": {"path": str(src)}}}
    disp = Dispatcher(services, probe_fn=fake_probe, prune_runner=fake_runner)
    outcome = disp.dispatch("jorder", payload)

    # prune saw the ORIGINAL S02E03 path (not the shifted S01E28), i.e. it ran before the move
    assert pruned_paths and "S02E03" in pruned_paths[0]
    assert all("S01E28" not in p for p in probe_paths)
    # shift still ran and moved the file afterward
    shift = next(s for s in outcome.steps if s.tool == "seasonal-shift")
    assert shift.result == OK
    assert (show_dir / "Season 1" / "MyShow - S01E28 - Title.mkv").exists()


class _RecJellyfin:
    """Stands in for JellyfinClient. ``item_id=None`` models a path Jellyfin doesn't know, which
    is what makes the pipeline fall back to a library scan."""

    def __init__(self, item_id: str | None = None):
        self.calls = 0  # refreshes of either kind
        self.item_refreshes: list[str] = []
        self.library_refreshes = 0
        self.item_id = item_id

    def refresh_path(self, local_path):
        if self.item_id is None:
            return None
        self.calls += 1
        self.item_refreshes.append(local_path)
        return self.item_id

    def refresh_library(self):
        self.calls += 1
        self.library_refreshes += 1
        return True


def test_ledger_recorded_even_when_prune_fails(tmp_path, monkeypatch):
    """Ledger capture now runs BEFORE prune, so a prune failure no longer loses the bandwidth."""
    from conftest import mk as _mk  # noqa: F401
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    services.qbit = _StubQbit({
        "name": "Mov", "downloaded": 500, "uploaded": 50, "total_size": 400,
        "category": "", "completion_on": 0, "added_on": 0, "state": "", "hash": "h",
    })
    payload = _radarr_payload(_touch(tmp_path))
    payload["payload"]["downloadId"] = "DEAD"
    disp = Dispatcher(
        services, probe_fn=lambda p: _dirty_probe(p),
        prune_runner=lambda a, **k: PruneResult(1, "", "boom"),  # prune fails -> raises
    )
    with pytest.raises(DispatchError):
        disp.dispatch("jlp", payload)
    # ledger still has the row despite the prune failure
    assert len(services.ledger.list()) == 1
    assert services.ledger.list()[0].downloaded_bytes == 500


def test_prune_cleans_stale_temp(tmp_path, monkeypatch):
    monkeypatch.setattr("mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, i, **k: None)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)
    # a leftover temp from a killed run
    stale = os.path.splitext(path)[0] + ".tmp.mkv"
    with open(stale, "w") as f:
        f.write("partial")
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p),
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("jst", _radarr_payload(path))
    assert not os.path.exists(stale)  # cleaned before the run


def test_jellyfin_refresh_on_ok_and_skip(tmp_path, monkeypatch):
    monkeypatch.setattr("mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, i, **k: None)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    jf = _RecJellyfin()
    services.jellyfin = jf
    # ok path
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p),
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("jok", _radarr_payload(_touch(tmp_path)))
    assert jf.calls == 1
    # skip path (clean file)
    clean = probe_of(mk(0, "video", codec_name="hevc"), mk(1, "audio", codec_name="dts", channels=6),
                     mk(2, "subtitle", codec_name="subrip", language="eng"))
    disp2 = Dispatcher(services, probe_fn=lambda p: clean, prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp2.dispatch("jsk", _radarr_payload(_touch(tmp_path)))
    assert jf.calls == 2  # refreshed on skip too


def test_jellyfin_not_called_on_prune_failure(tmp_path):
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    jf = _RecJellyfin()
    services.jellyfin = jf
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p),
                      prune_runner=lambda a, **k: PruneResult(1, "", "x"))
    with pytest.raises(DispatchError):
        disp.dispatch("jf", _radarr_payload(_touch(tmp_path)))
    assert jf.calls == 0


def test_quality_finding_detail_uses_titles(tmp_path):
    services = build_test_services(tmp_path, radarr_config(
        enable_seasonal_shift=False, enable_prune=False))
    services.config.ntfy.status_granularity = "off"
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p), prune_runner=lambda a, **k: None)
    disp.dispatch("jt", _radarr_payload(_touch(tmp_path)))
    qc = next(r for r in services.audit.for_job("jt") if r["tool"] == "quality-check")
    import json as _j
    detail = _j.loads(qc["after"])["detail"]
    assert "Foreign subtitles" in detail and detail != "A"  # human titles, not rule letters


def test_library_scan_job_indexes_without_touching_files(tmp_path):
    """The scan kind branches before the per-file pipeline: it probes, records one audit action,
    and leaves every file exactly as it found it."""
    from orchestrator.config import LibraryRoot
    from orchestrator.dispatch import LIBRARY_SCAN

    root = tmp_path / "lib"
    root.mkdir()
    movie = root / "movie.mkv"
    movie.write_bytes(b"x" * 500)
    before = movie.read_bytes()

    cfg = radarr_config()
    cfg.library.roots = [LibraryRoot(path=root, app="radarr")]
    cfg.library.min_size = "0"
    services = build_test_services(tmp_path, cfg)
    dispatcher = Dispatcher(services, probe_fn=_dirty_probe)

    steps: list[str] = []
    outcome = dispatcher.dispatch(
        "job-scan", {"root": None, "full": False}, kind=LIBRARY_SCAN, set_step=steps.append
    )

    assert steps == ["scan"]
    assert outcome.steps[0].result == OK
    assert "1 need work" in outcome.steps[0].detail
    assert movie.read_bytes() == before

    row = services.library.get(str(movie))
    assert row.needs_work == 1 and "subs" in row.reasons
    actions = services.audit.for_job("job-scan")
    assert [(a["tool"], a["action"]) for a in actions] == [("library", "scan")]


def _library_row_for(services, path: str):
    from orchestrator.library import LibraryRow

    services.library.upsert(
        LibraryRow(
            path=path, app="radarr", root=str(Path(path).parent), name=Path(path).name,
            size_bytes=1, mtime=0.0, needs_work=1, reasons="subs", summary="dropped 1 sub",
            drop_subs=1, enqueued_at="2026-01-01T00:00:00+00:00",
            scanned_at="2026-01-01T00:00:00.000000+00:00",
        )
    )


def test_prune_settles_the_library_row_it_came_from(tmp_path, monkeypatch):
    """A file queued from the Library tab must stop showing its pre-prune decision (and its
    'queued' badge) the moment the job finishes — without waiting for the next scan."""
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    _library_row_for(services, path)

    def runner(argv, **kw):
        return PruneResult(0, "", "")

    dispatcher = Dispatcher(services, probe_fn=_dirty_probe, prune_runner=runner)
    dispatcher.dispatch("job-1", _radarr_payload(path))

    row = services.library.get(path)
    assert row.needs_work == 0
    assert row.enqueued_at is None
    assert row.pruned_at is not None
    assert row.status == "done"


def test_a_failed_prune_leaves_the_library_row_alone(tmp_path, monkeypatch):
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    _library_row_for(services, path)

    def runner(argv, **kw):
        return PruneResult(2, "", "boom")

    dispatcher = Dispatcher(services, probe_fn=_dirty_probe, prune_runner=runner)
    with pytest.raises(DispatchError):
        dispatcher.dispatch("job-1", _radarr_payload(path))

    assert services.library.get(path).needs_work == 1


def test_a_remux_drops_the_row_for_the_old_filename(tmp_path, monkeypatch):
    """mp4 -> mkv writes a sibling and removes the source: the old path no longer exists, so its
    row goes and the next scan indexes the .mkv."""
    src = tmp_path / "movie.mp4"
    src.write_bytes(b"x")
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    _library_row_for(services, str(src))

    def runner(argv, **kw):
        (tmp_path / "movie.mkv").write_bytes(b"x")
        src.unlink()
        return PruneResult(0, "", "")

    dispatcher = Dispatcher(services, probe_fn=_dirty_probe, prune_runner=runner)
    dispatcher.dispatch("job-1", _radarr_payload(str(src)))

    assert services.library.get(str(src)) is None


# --- audio language filter, end to end ----------------------------------------------------
def _multi_dub_probe(path: str):
    return probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="ac3", channels=6, language="jpn"),
        mk(2, "audio", codec_name="ac3", channels=6, language="eng"),
        mk(3, "audio", codec_name="ac3", channels=6, language="fre"),
        mk(4, "audio", codec_name="ac3", channels=6, language="ger"),
        path=path,
    )


def _sonarr_payload(path: str, original: str | None = "Japanese") -> dict:
    series = {"id": 7, "title": "Some Anime"}
    if original:
        series["originalLanguage"] = {"id": 8, "name": original}
    return {
        "app": "sonarr",
        "payload": {
            "eventType": "Download",
            "series": series,
            "episodeFile": {"path": path},
            "episodes": [{"seasonNumber": 1, "episodeNumber": 3}],
        },
    }


def _filter_config():
    cfg = radarr_config(enable_seasonal_shift=False)
    cfg.policy.audio_language_filter = True
    cfg.policy.audio_keep_langs = {"eng", "ara"}
    return cfg


def test_webhook_original_language_reaches_the_mkvprune_argv(tmp_path):
    """The whole point: Sonarr says Japanese, so the French and German dubs are dropped and the
    Japanese + English tracks survive — with no arr API call anywhere in the path."""
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, _filter_config())
    seen: list[list[str]] = []

    def runner(argv, **kw):
        seen.append(argv)
        return PruneResult(0, "", "")

    disp = Dispatcher(services, probe_fn=_multi_dub_probe, prune_runner=runner)
    disp.dispatch("job-1", _sonarr_payload(path))

    assert seen, "mkvprune was never invoked"
    argv = seen[0]
    assert argv[argv.index("--audio") + 1] == "1,2"


def test_no_original_language_in_the_payload_keeps_every_track(tmp_path):
    """A payload without originalLanguage and no arr to ask must not narrow to eng/ara only."""
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, _filter_config())
    seen: list[list[str]] = []
    disp = Dispatcher(
        services,
        probe_fn=_multi_dub_probe,
        prune_runner=lambda argv, **kw: (seen.append(argv), PruneResult(0, "", ""))[1],
    )
    disp.dispatch("job-1", _sonarr_payload(path, original=None))
    # nothing to do at all: no subs to drop, no re-encode, and the filter has no opinion
    assert not seen


def test_a_rerun_falls_back_to_the_title_index(tmp_path):
    """A Library-tab re-run synthesizes an event with no originalLanguage, so the path index is
    what makes the filter work off the back catalogue."""
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, _filter_config())

    class FakeSonarr:
        def titles(self):
            return [{"path": str(tmp_path), "title": "Some Anime",
                     "originalLanguage": {"name": "Japanese"}}]

    services.arr_client = lambda app: FakeSonarr() if app == "sonarr" else None
    services.arr_instance = lambda app: None

    seen: list[list[str]] = []
    disp = Dispatcher(
        services,
        probe_fn=_multi_dub_probe,
        prune_runner=lambda argv, **kw: (seen.append(argv), PruneResult(0, "", ""))[1],
    )
    disp.dispatch("job-1", {"app": "sonarr", "payload": {
        "eventType": "Download", "series": {"title": ""},
        "episodeFile": {"path": path}, "episodes": []}})

    assert seen and seen[0][seen[0].index("--audio") + 1] == "1,2"


def test_the_filter_never_asks_the_arr_when_it_is_off(tmp_path):
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))

    def boom(app):
        raise AssertionError("the title index must not be consulted when the filter is off")

    services.arr_client = boom
    disp = Dispatcher(services, probe_fn=_multi_dub_probe,
                      prune_runner=lambda argv, **kw: PruneResult(0, "", ""))
    disp.dispatch("job-1", _sonarr_payload(path))


# --- jellyfin: refresh the file, not the whole library --------------------------------------
def test_jellyfin_refreshes_the_changed_item_not_the_library(tmp_path, monkeypatch):
    """A library scan is a file-presence walk: it never re-probes a file rewritten in place, so
    the pruned file must be refreshed by item id (the API behind "Refresh metadata")."""
    monkeypatch.setattr("mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, i, **k: None)
    path = _touch(tmp_path)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    jf = _RecJellyfin(item_id="abc123")
    services.jellyfin = jf

    disp = Dispatcher(services, probe_fn=_dirty_probe,
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("j1", _radarr_payload(path))

    assert jf.item_refreshes == [path]
    assert jf.library_refreshes == 0
    detail = next(a for a in services.audit.for_job("j1") if a["tool"] == "jellyfin")
    assert "abc123" in json.loads(detail["after"])["detail"]


def test_jellyfin_falls_back_to_a_scan_for_a_path_it_does_not_know(tmp_path, monkeypatch):
    """A brand-new import (or a renamed file) genuinely needs a scan — that is the one case the
    library refresh is right for."""
    monkeypatch.setattr("mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, i, **k: None)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    jf = _RecJellyfin(item_id=None)
    services.jellyfin = jf

    disp = Dispatcher(services, probe_fn=_dirty_probe,
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("j2", _radarr_payload(_touch(tmp_path)))

    assert jf.library_refreshes == 1 and jf.item_refreshes == []


def test_a_jellyfin_error_never_fails_the_job(tmp_path, monkeypatch):
    monkeypatch.setattr("mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, i, **k: None)
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))

    class Boom:
        def refresh_path(self, p):
            raise RuntimeError("jellyfin down")

        def refresh_library(self):
            raise RuntimeError("jellyfin down")

    services.jellyfin = Boom()
    disp = Dispatcher(services, probe_fn=_dirty_probe,
                      prune_runner=lambda a, **k: PruneResult(0, "", ""))
    disp.dispatch("j3", _radarr_payload(_touch(tmp_path)))  # must not raise
    rec = next(a for a in services.audit.for_job("j3") if a["tool"] == "jellyfin")
    assert rec["result"] == SKIP


def test_a_rerun_still_honours_a_per_title_override(tmp_path):
    """End to end for the shadowing bug: a Library-tab re-run carries no arr title, so the
    override has to be resolved through the path index. Without that, Star Wars: Visions loses
    its Japanese track on exactly the path the override exists for."""
    path = _touch(tmp_path)
    cfg = radarr_config(enable_seasonal_shift=False)
    cfg.policy.audio_language_filter = True
    cfg.policy.audio_keep_langs = {"eng", "ara"}
    cfg.policy.audio_original_overrides = {"Star Wars: Visions": ["jpn"]}
    services = build_test_services(tmp_path, cfg)

    class FakeSonarr:
        def titles(self):
            return [{"path": str(tmp_path), "title": "Star Wars: Visions",
                     "originalLanguage": {"name": "English"}}]

    services.arr_client = lambda app: FakeSonarr() if app == "sonarr" else None
    services.arr_instance = lambda app: None

    def probe(p):
        return probe_of(
            mk(0, "video", codec_name="hevc"),
            mk(1, "audio", codec_name="ac3", channels=6, language="eng"),
            mk(2, "audio", codec_name="ac3", channels=6, language="jpn"),
            path=p,
        )

    seen: list[list[str]] = []
    disp = Dispatcher(services, probe_fn=probe,
                      prune_runner=lambda a, **k: (seen.append(a), PruneResult(0, "", ""))[1])
    # the payload a Library-tab re-run builds: no title, no episodes
    disp.dispatch("j", {"app": "sonarr", "payload": {
        "eventType": "Download", "series": {"title": ""},
        "episodeFile": {"path": path}, "episodes": []}})

    assert not seen, "nothing should be dropped: eng is kept and jpn is the override original"

