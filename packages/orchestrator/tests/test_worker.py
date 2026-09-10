from __future__ import annotations

import time
import types

from conftest import FakeNotifier, build_test_services, mk, probe_of, radarr_config
from orchestrator.dispatch import Dispatcher
from orchestrator.queue import QUEUED
from orchestrator.runner import PruneResult
from orchestrator.worker import Worker


def test_worker_healthy_logic(tmp_path):
    services = build_test_services(tmp_path)
    w = Worker(services, healthy_after_s=30)
    # thread never started -> not alive -> not healthy
    assert w.healthy() is False
    w.is_alive = lambda: True  # type: ignore[method-assign]
    w._busy = False
    w.last_beat = time.monotonic()
    assert w.healthy() is True
    w.last_beat = time.monotonic() - 1000  # stale beat while idle -> unhealthy
    assert w.healthy() is False
    w._busy = True  # but a long in-flight job keeps it healthy
    assert w.healthy() is True


def _wan(services, host: str, total: int) -> None:
    """Record ``total`` bytes against a device: two polls, so the engine has a real difference."""
    from mediacore.router import DeviceStats

    mac = f"AA:{abs(hash(host)) % 256:02X}"
    base = services.wan.by_device()
    seen = sum(d.rx for d in base if d.host == host)
    services.wan.apply(
        [DeviceStats(mac=mac, host=host, kind="wired", rx=seen, tx=0, curr_rx=0, curr_tx=0)],
        link_max_bytes_s=10**12,
    )
    services.wan.apply(
        [DeviceStats(mac=mac, host=host, kind="wired", rx=seen + total, tx=0, curr_rx=0,
                     curr_tx=0)],
        link_max_bytes_s=10**12,
    )


def test_quota_alert_fires_once_per_threshold(tmp_path):
    services = build_test_services(tmp_path)
    services.notifier = FakeNotifier()
    services.quota_periods.add(start_date="2020-01-01", limit_bytes=1000)
    _wan(services, "server", 900)
    w = Worker(services)

    w._check_quota()  # 90% -> crosses 85
    assert len(services.notifier.sent) == 1
    w._check_quota()  # still 90% -> no new alert
    assert len(services.notifier.sent) == 1

    _wan(services, "server", 300)
    w._check_quota()  # 120% -> crosses 100
    assert len(services.notifier.sent) == 2
    assert services.quota_periods.latest().alerted_pct == 100


def test_the_quota_alert_measures_the_link_not_the_ledger(tmp_path):
    """The regression this rewrite exists for.

    Reading ``ledger.totals()`` counted what the pipeline downloaded and nothing else. Over one
    real period that was 409.05 GB against 505.36 GB actually consumed, so a household already
    over its 500 GB plan would have been told it was at 81%. A ledger row on its own must now
    raise nothing at all — it is a record of a download, not a measurement of the link.
    """
    services = build_test_services(tmp_path)
    services.notifier = FakeNotifier()
    services.quota_periods.add(start_date="2020-01-01", limit_bytes=1000)
    services.ledger.add_manual(
        name="dl", kind="movie", downloaded_bytes=5000, uploaded_bytes=0,
        completed_at="2020-06-01T00:00:00+00:00",
    )
    Worker(services)._check_quota()
    assert services.notifier.sent == []


def test_a_quota_with_an_allocation_alerts_on_its_own(tmp_path):
    from orchestrator.config import OrchestratorConfig

    cfg = OrchestratorConfig()
    cfg.quotas.people = {"youssef": {}}          # type: ignore[assignment]
    cfg.quotas.quotas = {"youssef": {"allocation": "1000"}}   # type: ignore[assignment]
    cfg.quotas.devices = {"PC-114": "youssef"}
    services = build_test_services(tmp_path, cfg)
    services.notifier = FakeNotifier()
    services.quota_periods.add(start_date="2020-01-01", limit_bytes=0)  # no plan limit
    _wan(services, "PC-114", 900)

    w = Worker(services)
    w._check_quota()
    assert [m.title for m in services.notifier.sent] == ["Youssef quota 90% used"]
    w._check_quota()  # once per threshold per period, per quota
    assert len(services.notifier.sent) == 1
    assert services.quota_periods.alerted_quotas(1) == {"youssef": 85}


def test_a_quota_with_no_allocation_never_alerts(tmp_path):
    """It is tracked, not bounded; inventing a threshold would page about an undecided pot."""
    services = build_test_services(tmp_path)
    services.notifier = FakeNotifier()
    services.quota_periods.add(start_date="2020-01-01", limit_bytes=0)
    _wan(services, "Xiaomi-TV-Box", 10**12)
    Worker(services)._check_quota()
    assert services.notifier.sent == []


def _dirty(path: str):
    return probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="opus", channels=6),
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),
        path=path,
    )


def test_low_space_defers_without_consuming_attempts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "mediacore.arr.client.RadarrClient.rescan_and_wait", lambda self, i, **k: None
    )
    # pretend the filesystem is full so the prune precheck defers the job
    monkeypatch.setattr(
        "orchestrator.steps.prune.shutil.disk_usage",
        lambda p: types.SimpleNamespace(total=100, used=100, free=0),
    )
    services = build_test_services(tmp_path, radarr_config(enable_seasonal_shift=False))
    services.notifier = FakeNotifier()
    f = tmp_path / "movie.mkv"
    f.write_bytes(b"x" * 10)
    payload = {
        "app": "radarr",
        "payload": {
            "eventType": "Download",
            "movie": {"id": 5, "title": "Mov", "year": 2021},
            "movieFile": {"path": str(f)},
        },
    }
    disp = Dispatcher(services, probe_fn=_dirty, prune_runner=lambda a, **k: PruneResult(0, "", ""))
    w = Worker(services, dispatcher=disp)

    jid = services.queue.enqueue("arr_event", payload, label="Mov")
    job = services.queue.claim_next()  # attempts -> 1
    assert job is not None

    for _ in range(3):  # repeated low-space runs must never dead-letter
        w._process_one(job.id, job.payload, job.label)
        j = services.queue.get(jid)
        assert j.status == QUEUED and j.attempts == 1  # deferred, attempt not consumed

    assert services.queue.claim_next() is None  # still deferred (not_before in the future)
    assert any("low disk space" in (m.title.lower()) for m in services.notifier.sent)
