from __future__ import annotations

from pathlib import Path

import pytest
from conftest import build_test_services, mk, probe_of
from mediacore.audit import OK, ActionRecord
from orchestrator import library as lib
from orchestrator.config import LibraryRoot, OrchestratorConfig
from orchestrator.library import LibraryIndex, LibraryRow


def _services(tmp_path: Path, root: Path, app: str = "radarr"):
    cfg = OrchestratorConfig()
    cfg.library.roots = [LibraryRoot(path=root, app=app)]
    cfg.library.min_size = "0"
    return build_test_services(tmp_path, cfg)


def _mkfile(path: Path, size: int = 200) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# --- the index -----------------------------------------------------------------------------
def _row(path: str, **kw) -> LibraryRow:
    base = {
        "path": path, "app": "radarr", "root": "/lib", "name": Path(path).name,
        "size_bytes": 1000, "mtime": 1.0, "scanned_at": "2026-01-01T00:00:00+00:00",
    }
    base.update(kw)
    return LibraryRow(**base)  # type: ignore[arg-type]


def test_index_upsert_and_filters(tmp_path) -> None:
    services = _services(tmp_path, tmp_path / "lib")
    index = services.library
    index.upsert(_row("/lib/a.mkv", needs_work=1, reasons="subs", delta_bytes=-10))
    index.upsert(_row("/lib/b.mkv", needs_work=1, reasons="audio", delta_bytes=-500))
    index.upsert(_row("/lib/c.mkv", needs_work=0, pruned_at="2026-01-01T00:00:00+00:00"))
    index.upsert(_row("/lib/d.mkv", error="boom"))

    rows, total = index.list()
    assert total == 4
    # default sort is impact: needs-work first, biggest saving first
    assert [r.name for r in rows][:2] == ["b.mkv", "a.mkv"]

    assert index.list(status="needs")[1] == 2
    assert index.list(status="clean")[1] == 0  # c.mkv is pruned, not merely clean
    assert index.list(status="done")[1] == 1
    assert index.list(status="error")[1] == 1
    assert index.list(reason="audio")[1] == 1
    assert index.list(q="b.mkv")[1] == 1
    assert index.list(app="sonarr")[1] == 0


def test_index_reason_match_is_exact(tmp_path) -> None:
    """'subs' must not match a row whose only reason is 'subsomething' (csv boundary)."""
    index = _services(tmp_path, tmp_path / "lib").library
    index.upsert(_row("/lib/a.mkv", needs_work=1, reasons="audio,remux"))
    assert index.list(reason="subs")[1] == 0
    assert index.list(reason="remux")[1] == 1


def test_index_counts_and_enqueue_marking(tmp_path) -> None:
    index = _services(tmp_path, tmp_path / "lib").library
    index.upsert(_row("/lib/a.mkv", needs_work=1, reasons="subs", drop_subs=2, delta_bytes=-10))
    index.upsert(_row("/lib/b.mkv", needs_work=1, reasons="audio", encode_audio=1, delta_bytes=-90))
    c = index.counts()
    assert index.counts()["delta_unknown"] == 0
    assert (c["total"], c["needs"], c["subs"], c["audio"], c["delta"]) == (2, 2, 1, 1, -100)
    assert c["delta_unknown"] == 0

    assert index.mark_enqueued(["/lib/a.mkv"]) == 1
    assert index.get("/lib/a.mkv").status == "queued"
    assert index.counts()["queued"] == 1


def test_index_actionable_totals_match_what_enqueue_would_queue(tmp_path) -> None:
    """The confirmation dialog quotes these numbers, so they must agree with paths_matching:
    actionable rows only, under the same filters."""
    services = _services(tmp_path, tmp_path / "lib")
    index = services.library
    index.upsert(_row("/lib/a.mkv", needs_work=1, reasons="subs", delta_bytes=-500))
    index.upsert(_row("/lib/b.mkv", needs_work=1, reasons="audio", delta_bytes=200, delta_known=0))
    index.upsert(_row("/lib/c.mkv", needs_work=0, delta_bytes=-9999))
    index.upsert(_row("/lib/d.mkv", error="boom", delta_bytes=-1))

    t = index.actionable_totals()
    assert t == {"count": 2, "delta": -300, "unknown": 1}
    assert t["count"] == len(index.paths_matching())

    assert index.actionable_totals(reason="subs") == {"count": 1, "delta": -500, "unknown": 0}
    assert index.actionable_totals(q="b.mkv") == {"count": 1, "delta": 200, "unknown": 1}
    assert index.actionable_totals(app="sonarr") == {"count": 0, "delta": 0, "unknown": 0}


def test_index_paths_matching_only_returns_work(tmp_path) -> None:
    index = _services(tmp_path, tmp_path / "lib").library
    index.upsert(_row("/lib/a.mkv", needs_work=1, reasons="subs"))
    index.upsert(_row("/lib/b.mkv", needs_work=0))
    assert index.paths_matching() == [("/lib/a.mkv", "radarr")]
    assert index.paths_matching(limit=0) == []


def test_prune_missing_is_scoped_to_the_walked_roots(tmp_path) -> None:
    index = _services(tmp_path, tmp_path / "lib").library
    index.upsert(_row("/lib/a.mkv", scanned_at="2026-01-01T00:00:00+00:00"))
    index.upsert(_row("/other/b.mkv", scanned_at="2026-01-01T00:00:00+00:00"))
    assert index.prune_missing(["/lib"], "2026-02-01T00:00:00+00:00") == 1
    assert index.get("/lib/a.mkv") is None
    assert index.get("/other/b.mkv") is not None  # a different root was never walked


# --- the estimate --------------------------------------------------------------------------
def _ffprobe(pr, streams: dict[int, dict]):
    """Attach an ffprobe payload shaped like the real thing: Matroska carries no per-track
    bitrate header, so the numbers live in mkvmerge's statistics tags."""
    pr.ffprobe_json = {
        "streams": [{"index": i, "tags": tags} for i, tags in streams.items()]
    }
    return pr


def _decision(pr):
    from quality_check.prune import decide

    return decide(pr, OrchestratorConfig().policy)


def test_track_sizes_prefers_the_exact_byte_count(tmp_path) -> None:
    pr = probe_of(mk(0, "video"), mk(1, "audio", codec_name="flac", channels=6))
    pr.duration_s = 1000.0
    _ffprobe(pr, {1: {"NUMBER_OF_BYTES": "500000000", "BPS": "1536000"}})
    assert lib.track_sizes(pr)[1] == 500_000_000  # the tag wins over the bitrate estimate


def test_track_sizes_falls_back_to_bps_then_to_ffprobe_bitrate() -> None:
    pr = probe_of(mk(0, "video"), mk(1, "audio"), mk(2, "audio"))
    pr.duration_s = 1000.0
    pr.ffprobe_json = {
        "streams": [
            {"index": 1, "tags": {"BPS": "1536000"}},
            {"index": 2, "bit_rate": "768000"},
        ]
    }
    sizes = lib.track_sizes(pr)
    assert sizes[1] == int(1_536_000 / 8 * 1000)
    assert sizes[2] == int(768_000 / 8 * 1000)


def test_track_sizes_skips_a_track_that_reveals_nothing() -> None:
    pr = probe_of(mk(0, "video"), mk(1, "audio"))
    pr.duration_s = 1000.0
    pr.ffprobe_json = {"streams": [{"index": 1, "tags": {"LANGUAGE": "eng"}}]}
    assert lib.track_sizes(pr) == {}


def test_estimate_counts_a_dropped_subtitle_as_an_exact_saving() -> None:
    """A dropped image sub is real reclaimed space — this is most of what the pipeline does, and
    it read as 'unknown' while the estimate only looked at ffprobe's (absent) bit_rate."""
    pr = probe_of(mk(0, "video"), mk(1, "subtitle", language="fre"))
    pr.duration_s = 1000.0
    _ffprobe(pr, {1: {"NUMBER_OF_BYTES": "1337835"}})
    assert lib.estimate_delta(pr, _decision(pr)) == (-1_337_835, True)


def test_estimate_trades_the_source_audio_for_the_ac3_track() -> None:
    pr = probe_of(mk(0, "video"), mk(1, "audio", codec_name="flac", channels=6))
    pr.duration_s = 1000.0
    _ffprobe(pr, {1: {"NUMBER_OF_BYTES": "192000000"}})
    delta, known = lib.estimate_delta(pr, _decision(pr))
    assert known
    assert delta == int(640_000 / 8 * 1000) - 192_000_000  # 640k AC-3 replaces the FLAC bytes


def test_estimate_reports_the_compat_downmix_as_growth() -> None:
    """A >6-channel track keeps the original AND gains a 5.1 AC-3 downmix, so the file grows."""
    pr = probe_of(mk(0, "video"), mk(1, "audio", codec_name="flac", channels=8))
    pr.duration_s = 1000.0
    _ffprobe(pr, {1: {"NUMBER_OF_BYTES": "192000000"}})
    delta, known = lib.estimate_delta(pr, _decision(pr))
    assert known and delta == int(640_000 / 8 * 1000)


def test_estimate_is_a_floor_when_only_some_tracks_report_a_size() -> None:
    """Partial data yields a partial number flagged unknown — never a confident zero, and never
    a total that silently omits the tracks it could not measure."""
    pr = probe_of(
        mk(0, "video"),
        mk(1, "audio", codec_name="flac", channels=6),
        mk(2, "subtitle", language="fre"),
    )
    pr.duration_s = 1000.0
    _ffprobe(pr, {2: {"NUMBER_OF_BYTES": "1000000"}})  # audio track reveals nothing
    delta, known = lib.estimate_delta(pr, _decision(pr))
    assert not known
    assert delta == -1_000_000


def test_estimate_is_unknown_with_no_track_sizes_at_all() -> None:
    pr = probe_of(mk(0, "video"), mk(1, "subtitle", language="fre"))
    pr.duration_s = 1000.0
    assert lib.estimate_delta(pr, _decision(pr)) == (0, False)


def test_estimate_is_known_and_zero_for_a_clean_file() -> None:
    pr = probe_of(mk(0, "video"), mk(1, "audio", codec_name="ac3", channels=6))
    pr.duration_s = 1000.0
    assert lib.estimate_delta(pr, _decision(pr)) == (0, True)


# --- the scan ------------------------------------------------------------------------------
def _probe_fn(needs_work_for: set[str]):
    def fn(path: str):
        tracks = [mk(0, "video")]
        if path in needs_work_for:
            tracks.append(mk(1, "subtitle", language="fre"))
        pr = probe_of(*tracks, path=path)
        pr.duration_s = 100.0
        return pr

    return fn


def test_scan_indexes_and_skips_unchanged(tmp_path) -> None:
    root = tmp_path / "lib"
    a = _mkfile(root / "Movie A" / "a.mkv")
    b = _mkfile(root / "Movie B" / "b.mkv")
    services = _services(tmp_path, root)

    stats = lib.scan(services, probe_fn=_probe_fn({str(a)}))
    assert (stats.files, stats.probed, stats.needs_work) == (2, 2, 1)
    assert services.library.get(str(a)).reasons == "subs"
    assert services.library.get(str(b)).needs_work == 0

    # second pass: nothing changed on disk, so nothing is re-probed
    again = lib.scan(services, probe_fn=_probe_fn({str(a)}))
    assert (again.probed, again.cached, again.needs_work) == (0, 2, 1)

    # ...unless asked for a full re-probe
    forced = lib.scan(services, full=True, probe_fn=_probe_fn({str(a)}))
    assert forced.probed == 2


def test_scan_reprobes_a_changed_file(tmp_path) -> None:
    root = tmp_path / "lib"
    a = _mkfile(root / "a.mkv")
    services = _services(tmp_path, root)
    lib.scan(services, probe_fn=_probe_fn({str(a)}))

    a.write_bytes(b"y" * 400)  # new size + mtime
    stats = lib.scan(services, probe_fn=_probe_fn(set()))
    assert stats.probed == 1
    assert services.library.get(str(a)).needs_work == 0


def test_scan_drops_rows_for_deleted_files(tmp_path) -> None:
    root = tmp_path / "lib"
    a = _mkfile(root / "a.mkv")
    _mkfile(root / "b.mkv")
    services = _services(tmp_path, root)
    lib.scan(services, probe_fn=_probe_fn(set()))

    a.unlink()
    stats = lib.scan(services, probe_fn=_probe_fn(set()))
    assert stats.removed == 1
    assert services.library.get(str(a)) is None


def test_scan_records_a_probe_failure_without_stopping(tmp_path) -> None:
    root = tmp_path / "lib"
    bad = _mkfile(root / "bad.mkv")
    _mkfile(root / "good.mkv")
    services = _services(tmp_path, root)

    def fn(path: str):
        if path == str(bad):
            raise RuntimeError("not a matroska file")
        return probe_of(mk(0, "video"), path=path)

    stats = lib.scan(services, probe_fn=fn)
    assert (stats.files, stats.errors) == (2, 1)
    row = services.library.get(str(bad))
    assert row.status == "error" and "matroska" in row.error


def test_scan_ignores_non_media_temp_and_tiny_files(tmp_path) -> None:
    root = tmp_path / "lib"
    _mkfile(root / "keep.mkv", size=500)
    _mkfile(root / "art.jpg", size=500)
    _mkfile(root / "keep.tmp.mkv", size=500)
    _mkfile(root / "sample.mkv", size=10)
    cfg = OrchestratorConfig()
    cfg.library.roots = [LibraryRoot(path=root, app="radarr")]
    cfg.library.min_size = "100B"
    services = build_test_services(tmp_path, cfg)

    stats = lib.scan(services, probe_fn=_probe_fn(set()))
    assert stats.files == 1
    assert services.library.get(str(root / "keep.mkv")) is not None


def test_scan_marks_a_file_the_pipeline_already_pruned(tmp_path) -> None:
    root = tmp_path / "lib"
    a = _mkfile(root / "a.mkv")
    services = _services(tmp_path, root)
    services.audit.record(
        ActionRecord(
            tool="mkvprune", action="prune", result=OK, path=str(a), mtime=a.stat().st_mtime
        )
    )
    lib.scan(services, probe_fn=_probe_fn(set()))
    row = services.library.get(str(a))
    assert row.pruned_at is not None and row.status == "done"


def test_scan_can_be_narrowed_to_one_folder(tmp_path) -> None:
    root = tmp_path / "lib"
    _mkfile(root / "showA" / "a.mkv")
    b = _mkfile(root / "showB" / "b.mkv")
    services = _services(tmp_path, root)

    stats = lib.scan(services, root=str(root / "showB"), probe_fn=_probe_fn(set()))
    assert stats.files == 1
    assert services.library.get(str(b)) is not None
    # ...and scanning one folder must not evict the rest of the index
    lib.scan(services, probe_fn=_probe_fn(set()))
    stats = lib.scan(services, root=str(root / "showB"), probe_fn=_probe_fn(set()))
    assert stats.removed == 0
    assert services.library.get(str(root / "showA" / "a.mkv")) is not None

    with pytest.raises(ValueError):
        lib.scan(services, root="/elsewhere", probe_fn=_probe_fn(set()))


def test_scan_stops_when_the_worker_is_paused(tmp_path) -> None:
    root = tmp_path / "lib"
    for i in range(60):
        _mkfile(root / f"f{i:02d}.mkv")
    services = _services(tmp_path, root)
    services.queue.pause()

    stats = lib.scan(services, probe_fn=_probe_fn(set()))
    assert stats.stopped
    assert stats.probed < stats.files
    assert stats.removed == 0  # a partial scan must never evict what it didn't reach


def test_resolve_roots_falls_back_to_the_arr_mounts(tmp_path) -> None:
    from mediacore.models import ArrInstance, CoreConfig

    cfg = OrchestratorConfig()
    cfg.arr = CoreConfig().arr
    cfg.arr.radarr = ArrInstance(
        base_url="http://radarr", api_key="k",
        media_root=Path("/data/movies"), local_media_root=Path("/media/movies"),
    )
    cfg.arr.sonarr = ArrInstance(base_url="http://sonarr", api_key="k")
    # the local mount wins; an instance with no roots configured contributes nothing
    assert lib.resolve_roots(cfg) == [(Path("/media/movies"), "radarr")]


def test_index_migrate_is_idempotent(tmp_path) -> None:
    services = _services(tmp_path, tmp_path / "lib")
    LibraryIndex(services.db)
    LibraryIndex(services.db)
    assert services.library.list()[1] == 0


def test_disabled_library_has_no_roots(tmp_path) -> None:
    services = _services(tmp_path, tmp_path / "lib")
    services.config.library.enabled = False
    assert lib.resolve_roots(services.config) == []
    assert lib.scan(services, probe_fn=_probe_fn(set())).files == 0


def test_min_size_accepts_a_bare_number() -> None:
    from orchestrator.config import LibraryConfig

    assert LibraryConfig(min_size=0).min_size_bytes == 0
    assert LibraryConfig(min_size="100MB").min_size_bytes == 100_000_000


def test_a_formula_change_invalidates_cached_rows(tmp_path) -> None:
    """A row computed by an older estimator must be re-probed by an ordinary scan. Without the
    version stamp, a fixed formula would only ever reach files that happened to change on disk."""
    root = tmp_path / "lib"
    _mkfile(root / "a.mkv")
    services = _services(tmp_path, root)
    lib.scan(services, probe_fn=_probe_fn(set()))
    assert lib.scan(services, probe_fn=_probe_fn(set())).probed == 0  # cached

    services.db.execute("UPDATE library_files SET scan_version = 0")
    services.db.commit()
    assert lib.scan(services, probe_fn=_probe_fn(set())).probed == 1


def test_reconcile_clears_the_flag_for_a_job_that_left_the_queue(tmp_path) -> None:
    """Cancelling or deleting a queued job never calls mark_clean, so the 'queued' flag has to be
    re-derived from the live queue or the badge and the tile stick forever."""
    index = _services(tmp_path, tmp_path / "lib").library
    index.upsert(_row("/lib/a.mkv", needs_work=1, reasons="subs"))
    index.upsert(_row("/lib/b.mkv", needs_work=1, reasons="subs"))
    index.mark_enqueued(["/lib/a.mkv", "/lib/b.mkv"])
    assert index.counts()["queued"] == 2

    # only a.mkv still has a job in flight
    assert index.reconcile_enqueued({"/lib/a.mkv"}) == 1
    assert index.counts()["queued"] == 1
    assert index.get("/lib/a.mkv").status == "queued"
    assert index.get("/lib/b.mkv").status == "needs"

    # an empty queue clears everything
    assert index.reconcile_enqueued(set()) == 1
    assert index.counts()["queued"] == 0


def test_scanning_a_missing_folder_is_an_error_not_an_empty_success(tmp_path) -> None:
    """A typo'd folder must not report a clean scan of zero files — that reads as 'all fine'."""
    root = tmp_path / "lib"
    _mkfile(root / "showA" / "a.mkv")
    services = _services(tmp_path, root)
    with pytest.raises(ValueError, match="does not exist"):
        lib.scan(services, root=str(root / "typo"), probe_fn=_probe_fn(set()))
