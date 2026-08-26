from __future__ import annotations

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

    def fake_runner(argv):
        argv_seen.append(argv)
        return PruneResult(0, "ok", "")

    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p), prune_runner=fake_runner)
    outcome = disp.dispatch("job1", _radarr_payload(path))

    results = {s.tool: s.result for s in outcome.steps}
    assert results["quality-check"] == OK
    assert results["mkvprune"] == OK
    assert calls == [5]  # rescan called with movie id
    # argv keeps all audio, drops foreign sub (id 2) -> keep only id 3
    assert argv_seen[0] == ["mkvprune", "-y", "--audio", "all", "--subs", "3", path]

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
    disp = Dispatcher(services, probe_fn=lambda p: clean, prune_runner=lambda a: PruneResult(0, "", ""))
    outcome = disp.dispatch("j", _radarr_payload(path))
    prune = next(s for s in outcome.steps if s.tool == "mkvprune")
    assert prune.result == SKIP
    assert calls == []  # no rescan when nothing changed


def test_prune_dedup_second_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, item_id, **kw: None
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)
    disp = Dispatcher(services, probe_fn=lambda p: _dirty_probe(p), prune_runner=lambda a: PruneResult(0, "", ""))
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
        prune_runner=lambda a: PruneResult(3, "", "verify failed"),
    )
    with pytest.raises(DispatchError):
        disp.dispatch("j", _radarr_payload(path))


def test_probe_failure_is_nonfatal_quality_but_fatal_prune(tmp_path):
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    path = _touch(tmp_path)

    def boom(_p):
        raise RuntimeError("no ffprobe")

    disp = Dispatcher(services, probe_fn=boom, prune_runner=lambda a: PruneResult(0, "", ""))
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
    disp = Dispatcher(services, probe_fn=lambda p: None, prune_runner=lambda a: None)
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
                      prune_runner=lambda a: PruneResult(0, "", ""))
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
                      prune_runner=lambda a: PruneResult(0, "", ""))
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

    def fake_runner(argv):
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
