from __future__ import annotations

from pathlib import Path

from mediacore.dashdot import SystemSnapshot
from mediacore.docker_status import ContainerStatus
from mediacore.scrutiny import DriveStatus
from orchestrator.alerts import CRIT, OK, WARN
from orchestrator.config import AlertRule, DiskWatch, DockerSource
from orchestrator.monitor import (
    collect_containers,
    collect_disks,
    collect_smart,
    collect_system,
    read_disk,
    scalar_severity,
)
from orchestrator.monitor.disk import _disk_severity
from orchestrator.monitor.docker import _container_severity


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


class FakeDashdot:
    def __init__(self, snap) -> None:
        self._snap = snap

    def snapshot(self):
        return self._snap


def testscalar_severity() -> None:
    rule = AlertRule(warn=75, crit=85)
    assert scalar_severity(50, rule) == OK
    assert scalar_severity(80, rule) == WARN
    assert scalar_severity(90, rule) == CRIT


def _snap(cpu_temp=70.0, mem_used=8, mem_total=16):
    return SystemSnapshot(
        cpu_load=20.0, cpu_temp=cpu_temp, core_count=2, mem_used=mem_used,
        mem_total=mem_total, net_up=1, net_down=2, uptime_s=100.0, cpu_model="X",
    )


def test_collect_system_samples_and_alerts() -> None:
    temp_rule = AlertRule(warn=75, crit=85)
    mem_rule = AlertRule(warn=90, crit=97)
    # mem 15/16 ≈ 94% -> warn; temp 88 -> crit
    client = FakeDashdot(_snap(cpu_temp=88.0, mem_used=15, mem_total=16))
    samples, evals, snap = collect_system(client, temp_rule, mem_rule)
    assert snap is not None
    names = {s.name for s in samples}
    assert {"cpu.load", "mem.pct", "mem.used", "net.up", "net.down", "cpu.temp"} <= names
    by_key = {e.key: e.severity for e in evals}
    assert by_key["cpu_temp"] == CRIT
    assert by_key["mem"] == WARN


def test_collect_system_no_temp_skips_temp_eval() -> None:
    client = FakeDashdot(_snap(cpu_temp=None))
    _s, evals, _out = collect_system(client, AlertRule(warn=75), AlertRule(warn=90))
    assert "cpu_temp" not in {e.key for e in evals}
    assert "mem" in {e.key for e in evals}


def test_collect_system_unreachable() -> None:
    client = FakeDashdot(None)
    samples, evals, snap = collect_system(client, AlertRule(), AlertRule())
    assert samples == [] and evals == [] and snap is None


class FakeScrutiny:
    def __init__(self, drives) -> None:
        self._drives = drives

    def drives(self):
        return self._drives


def test_collect_smart_health_and_temp() -> None:
    health = AlertRule(sustain_s=0)
    temp = AlertRule(warn=50, crit=60)
    drives = [
        DriveStatus(name="sda", model="Seagate", passed=True, temp=38, power_on_hours=12000),
        DriveStatus(name="nvme0n1", model="Samsung", passed=False, temp=62, power_on_hours=8000),
    ]
    samples, evals, out = collect_smart(FakeScrutiny(drives), health, temp)
    assert out is not None
    assert {"smart.temp:sda", "smart.temp:nvme0n1"} == {s.name for s in samples}
    by_key = {e.key: e.severity for e in evals}
    assert by_key["smart:sda"] == OK and by_key["smart:nvme0n1"] == CRIT  # failed drive
    assert by_key["smart_temp:sda"] == OK and by_key["smart_temp:nvme0n1"] == CRIT  # 62 >= 60


def test_collect_smart_no_temp_skips_temp_eval() -> None:
    drives = [DriveStatus(name="sdb", model="X", passed=True, temp=None, power_on_hours=None)]
    samples, evals, _out = collect_smart(FakeScrutiny(drives), AlertRule(sustain_s=0), AlertRule(warn=50))
    assert samples == []  # no temp sample
    assert {e.key for e in evals} == {"smart:sdb"}  # health only, no temp eval


def test_collect_smart_unreachable() -> None:
    samples, evals, out = collect_smart(FakeScrutiny(None), AlertRule(), AlertRule())
    assert samples == [] and evals == [] and out is None


# --- the source registry -------------------------------------------------------------------
def _mon(**kw):
    from orchestrator.config import MonitoringConfig

    return MonitoringConfig(**kw)


def test_build_sources_lists_every_source_enabled_or_not() -> None:
    """A disabled source stays in the list so the UI can tell "switched off" from "never
    collected" without a second lookup."""
    from orchestrator.monitor import build_sources

    names = [s.name for s in build_sources(_mon())]
    assert names == ["disk", "docker", "system", "smart"]


def test_only_configured_sources_report_enabled() -> None:
    from orchestrator.config import DockerSource, SmartSource, SystemSource
    from orchestrator.monitor import build_sources

    off = {s.name: s.enabled for s in build_sources(_mon())}
    assert off == {"disk": True, "docker": False, "system": False, "smart": False}

    on = {
        s.name: s.enabled
        for s in build_sources(
            _mon(
                docker=DockerSource(enabled=True),
                system=SystemSource(enabled=True),
                smart=SmartSource(enabled=True),
            )
        )
    }
    assert all(on.values())


def test_each_source_reports_its_own_interval() -> None:
    from orchestrator.config import SmartSource
    from orchestrator.monitor import build_sources

    by_name = {s.name: s for s in build_sources(_mon(disk_interval_s=11, smart=SmartSource(interval_s=999)))}
    assert by_name["disk"].interval_s == 11
    assert by_name["smart"].interval_s == 999


class _FakeSource:
    """A source that reports whatever it is handed, and counts its collections."""

    name = "fake"
    silent = False

    def __init__(self, data, *, interval_s: int = 0):
        self._data = data
        self._interval = interval_s
        self.calls = 0

    @property
    def enabled(self) -> bool:
        return True

    @property
    def interval_s(self) -> int:
        return self._interval

    def collect(self):
        self.calls += 1
        return [], [], self._data


def test_the_worker_records_a_snapshot_and_its_reachability(tmp_path) -> None:
    """``data is None`` means the service is unreachable, and must be recorded as such — leaving
    the last good numbers on screen would show them as if they were live."""
    from conftest import build_test_services
    from orchestrator.config import OrchestratorConfig
    from orchestrator.worker import Worker

    services = build_test_services(tmp_path, OrchestratorConfig())
    worker = Worker(services)

    good = _FakeSource(["a", "b"])
    worker._collect(good)
    assert services.snapshot("fake").ok is True
    assert services.snapshot("fake").rows == ["a", "b"]

    worker._collect(_FakeSource(None))
    assert services.snapshot("fake").ok is False
    assert services.snapshot("fake").rows == []

    # a source nobody has collected yet is neither ok nor failed
    assert services.snapshot("never-heard-of-it").ok is None


def test_a_source_is_only_collected_once_per_interval(tmp_path) -> None:
    """One generic gate replaced four hand-rolled _last_* timers."""
    from conftest import build_test_services
    from orchestrator.config import OrchestratorConfig
    from orchestrator.worker import Worker

    services = build_test_services(tmp_path, OrchestratorConfig())
    src = _FakeSource(["x"], interval_s=3600)
    services.sources = [src]

    worker = Worker(services)
    worker._maybe_collect()
    worker._maybe_collect()
    worker._maybe_collect()
    assert src.calls == 1  # the first tick collects; the rest are inside the interval


def test_a_disabled_source_is_never_collected(tmp_path) -> None:
    from conftest import build_test_services
    from orchestrator.config import OrchestratorConfig
    from orchestrator.worker import Worker

    class Off(_FakeSource):
        @property
        def enabled(self) -> bool:
            return False

    services = build_test_services(tmp_path, OrchestratorConfig())
    src = Off(["x"])
    services.sources = [src]
    Worker(services)._maybe_collect()
    assert src.calls == 0
