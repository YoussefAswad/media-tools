from __future__ import annotations

from pathlib import Path

from mediacore.docker_status import ContainerStatus
from orchestrator.alerts import CRIT, OK, WARN
from orchestrator.collectors import (
    _container_severity,
    _disk_severity,
    collect_containers,
    collect_disks,
    read_disk,
)
from orchestrator.config import AlertRule, DiskWatch, DockerSource


class FakeDocker:
    def __init__(self, containers) -> None:
        self._c = containers

    def containers(self):
        return self._c


def _cs(name, state="running", health=None):
    return ContainerStatus(name=name, state=state, status="x", health=health, up=(state == "running"))


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


def test_container_severity() -> None:
    assert _container_severity(_cs("a")) == OK
    assert _container_severity(_cs("a", health="healthy")) == OK
    assert _container_severity(_cs("a", health="starting")) == OK
    assert _container_severity(_cs("a", health="unhealthy")) == WARN
    assert _container_severity(_cs("a", state="exited")) == CRIT


def test_collect_containers_watch_filter() -> None:
    docker = FakeDocker([_cs("radarr"), _cs("sonarr", state="exited"), _cs("ntfy")])
    cfg = DockerSource(enabled=True, watch=["sonarr"])
    samples, evals, containers = collect_containers(docker, cfg, AlertRule())
    # only the watched container is evaluated; all three are returned for the UI
    assert [e.key for e in evals] == ["container:sonarr"]
    assert evals[0].severity == CRIT
    assert containers is not None and len(containers) == 3
    names = {s.name for s in samples}
    assert names == {"docker.containers_up", "docker.containers_total"}
    up = next(s.value for s in samples if s.name == "docker.containers_up")
    assert up == 2.0


def test_collect_containers_empty_watch_evaluates_all() -> None:
    docker = FakeDocker([_cs("a"), _cs("b", state="exited")])
    _s, evals, _c = collect_containers(docker, DockerSource(enabled=True), AlertRule())
    assert {e.key for e in evals} == {"container:a", "container:b"}


def test_collect_containers_unreachable() -> None:
    docker = FakeDocker(None)
    samples, evals, containers = collect_containers(docker, DockerSource(enabled=True), AlertRule())
    assert samples == [] and evals == [] and containers is None
