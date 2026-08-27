from __future__ import annotations

from pathlib import Path

from orchestrator.alerts import CRIT, OK, WARN
from orchestrator.collectors import _disk_severity, collect_disks, read_disk
from orchestrator.config import AlertRule, DiskWatch


def test_read_disk_real_path(tmp_path) -> None:
    rep = read_disk(DiskWatch(path=tmp_path, label="TMP"))
    assert rep is not None
    assert rep.total > 0
    assert 0 <= rep.pct <= 100
    assert rep.used + rep.free <= rep.total + 1  # rounding slack


def test_read_disk_missing_path() -> None:
    assert read_disk(DiskWatch(path=Path("/no/such/mount/xyz"), label="X")) is None


def test_disk_severity_pct_thresholds() -> None:
    w = DiskWatch(path=Path("/x"), label="X", warn_pct=80, crit_pct=95, crit_free=None)
    assert _disk_severity(w, pct=50, free=10**12) == OK
    assert _disk_severity(w, pct=85, free=10**12) == WARN
    assert _disk_severity(w, pct=96, free=10**12) == CRIT


def test_disk_severity_free_thresholds() -> None:
    # pct is comfortable, but absolute free space trips the alert.
    w = DiskWatch(
        path=Path("/x"), label="X", warn_pct=None, crit_pct=None,
        warn_free="200GB", crit_free="50GB",
    )
    assert _disk_severity(w, pct=10, free=300 * 10**9) == OK
    assert _disk_severity(w, pct=10, free=150 * 10**9) == WARN
    assert _disk_severity(w, pct=10, free=40 * 10**9) == CRIT


def test_collect_disks_shapes(tmp_path) -> None:
    disks = [DiskWatch(path=tmp_path, label="TMP")]
    samples, evals = collect_disks(disks, AlertRule())
    names = {s.name for s in samples}
    assert names == {
        f"disk.used:{tmp_path}",
        f"disk.free:{tmp_path}",
        f"disk.total:{tmp_path}",
        f"disk.pct:{tmp_path}",
    }
    assert len(evals) == 1
    assert evals[0].key == f"disk:{tmp_path}"
    assert evals[0].label == "TMP"


def test_collect_disks_skips_unreadable(tmp_path) -> None:
    disks = [
        DiskWatch(path=tmp_path, label="OK"),
        DiskWatch(path=Path("/no/such/mount/xyz"), label="GONE"),
    ]
    _samples, evals = collect_disks(disks, AlertRule())
    assert len(evals) == 1  # the missing one is skipped, not fatal
    assert evals[0].label == "OK"
