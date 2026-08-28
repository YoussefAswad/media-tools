from __future__ import annotations

import time
import types

from conftest import build_test_services, mk, probe_of, radarr_config
from orchestrator.dispatch import Dispatcher
from orchestrator.queue import QUEUED
from orchestrator.runner import PruneResult
from orchestrator.worker import Worker


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, msg) -> bool:
        self.sent.append(msg)
        return True


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


def test_quota_alert_fires_once_per_threshold(tmp_path):
    services = build_test_services(tmp_path)
    services.notifier = FakeNotifier()
    services.cutoffs.add(cutoff_date="2020-01-01", limit_bytes=1000)
    services.ledger.add_manual(
        name="dl", kind="movie", downloaded_bytes=900, uploaded_bytes=0,
        completed_at="2020-06-01T00:00:00+00:00",
    )
    w = Worker(services)

    w._check_quota()  # 90% -> crosses 85
    assert len(services.notifier.sent) == 1
    w._check_quota()  # still 90% -> no new alert
    assert len(services.notifier.sent) == 1

    services.ledger.add_manual(
        name="dl2", kind="movie", downloaded_bytes=300, uploaded_bytes=0,
        completed_at="2020-06-02T00:00:00+00:00",
    )
    w._check_quota()  # 120% -> crosses 100
    assert len(services.notifier.sent) == 2
    assert services.cutoffs.latest().alerted_pct == 100


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
