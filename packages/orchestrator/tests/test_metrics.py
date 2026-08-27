from __future__ import annotations

import datetime as dt

from mediacore.audit import open_db
from orchestrator.metrics import Metrics, Sample


def _db(tmp_path):
    return open_db(tmp_path / "state")


def test_record_and_latest(tmp_path) -> None:
    m = Metrics(_db(tmp_path))
    m.record_many([Sample("disk.pct:/data", 40.0), Sample("disk.pct:/data", 55.5)])
    latest = m.latest("disk.pct:/data")
    assert latest is not None
    assert latest[1] == 55.5
    assert m.latest("nope") is None


def test_record_many_empty_is_noop(tmp_path) -> None:
    m = Metrics(_db(tmp_path))
    m.record_many([])
    assert m.latest("x") is None


def test_series_window_and_order(tmp_path) -> None:
    m = Metrics(_db(tmp_path))
    now = dt.datetime.now(dt.UTC)
    old = (now - dt.timedelta(days=40)).isoformat(timespec="seconds")
    recent = (now - dt.timedelta(days=1)).isoformat(timespec="seconds")
    m.record_many(
        [
            Sample("disk.used:/data", 100.0, ts=old),
            Sample("disk.used:/data", 200.0, ts=recent),
        ]
    )
    s = m.series("disk.used:/data", since_days=7)
    assert s == [(recent, 200.0)]  # the 40-day-old sample is outside the window


def test_daily_max(tmp_path) -> None:
    m = Metrics(_db(tmp_path))
    now = dt.datetime.now(dt.UTC)
    day = (now - dt.timedelta(days=1)).replace(hour=3).isoformat(timespec="seconds")
    day2 = (now - dt.timedelta(days=1)).replace(hour=20).isoformat(timespec="seconds")
    m.record_many([Sample("d", 10.0, ts=day), Sample("d", 30.0, ts=day2)])
    daily = m.daily_max("d", since_days=7)
    assert len(daily) == 1
    assert daily[0][1] == 30.0  # MAX within the day


def test_prune_old(tmp_path) -> None:
    m = Metrics(_db(tmp_path))
    now = dt.datetime.now(dt.UTC)
    old = (now - dt.timedelta(days=40)).isoformat(timespec="seconds")
    m.record_many([Sample("d", 1.0, ts=old), Sample("d", 2.0)])
    assert m.prune_old(30) == 1
    assert m.prune_old(0) == 0
    assert len(m.series("d", since_days=365)) == 1
