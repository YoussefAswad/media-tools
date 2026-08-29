"""The WAN delta engine: turning the router's cumulative counters into a durable daily record.

Every rule here exists because of a real observed case on the Archer VR600, so the tests are
written against those cases rather than against the implementation.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from mediacore.db import open_db
from mediacore.router import DeviceStats
from orchestrator.wan_usage import WanUsage

LINK = 25_000_000  # bytes/sec ceiling used for the plausibility bound

_T0 = _dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.UTC)


def _dev(mac="AA:BB", host="host", rx=0, tx=0, kind="wired") -> DeviceStats:
    return DeviceStats(mac=mac, host=host, kind=kind, rx=rx, tx=tx, curr_rx=0, curr_tx=0)


@pytest.fixture
def usage(tmp_path) -> WanUsage:
    return WanUsage(open_db(tmp_path))


def test_cold_start_baselines_and_counts_nothing(usage: WanUsage) -> None:
    """The first poll must not credit a device's whole accumulated history to today."""
    result = usage.apply([_dev(rx=9_000_000_000, tx=500_000)], link_max_bytes_s=LINK, now=_T0)
    assert result.cold_start is True
    assert result.counted == 0
    assert usage.totals()["total"] == 0


def test_the_second_poll_counts_the_difference(usage: WanUsage) -> None:
    usage.apply([_dev(rx=1_000, tx=500)], link_max_bytes_s=LINK, now=_T0)
    usage.apply(
        [_dev(rx=61_000, tx=1_500)], link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(seconds=60)
    )
    totals = usage.totals()
    assert totals["downloaded"] == 60_000
    assert totals["uploaded"] == 1_000


def test_a_new_device_mid_run_counts_its_current_value(usage: WanUsage) -> None:
    """Once a cursor exists, a device appearing is new to the router too, so its counter is
    genuinely new traffic — unlike at cold start."""
    usage.apply([_dev(mac="AA:BB", rx=100)], link_max_bytes_s=LINK, now=_T0)
    later = _T0 + _dt.timedelta(seconds=60)
    usage.apply(
        [_dev(mac="AA:BB", rx=100), _dev(mac="CC:DD", host="phone", rx=5_000)],
        link_max_bytes_s=LINK,
        now=later,
    )
    by_mac = {d.mac: d for d in usage.by_device()}
    assert by_mac["CC:DD"].rx == 5_000


def test_a_counter_going_backwards_is_a_reset_not_a_wrap(usage: WanUsage) -> None:
    """A 32-bit wrap needs 4.29 GB; at any plausible link speed that cannot happen inside one
    interval, so backwards means the router rebooted and the current value is the new traffic."""
    usage.apply([_dev(rx=5_000_000)], link_max_bytes_s=LINK, now=_T0)
    result = usage.apply(
        [_dev(rx=42_000)], link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(seconds=60)
    )
    assert result.resets == 1
    assert usage.totals()["downloaded"] == 42_000  # not a negative, and not 4 GiB + delta


def test_an_implausible_delta_is_dropped_and_rebaselined(usage: WanUsage) -> None:
    """Measured: the router reports a garbage record right after a settings change — 27 GB in a
    single minute. Counting it would poison the period's total permanently."""
    usage.apply([_dev(rx=1_000)], link_max_bytes_s=LINK, now=_T0)
    t1 = _T0 + _dt.timedelta(seconds=60)
    result = usage.apply([_dev(rx=27_283_731_946)], link_max_bytes_s=LINK, now=t1)
    assert result.rejected == 1
    assert usage.totals()["total"] == 0

    # Re-baselined, so the *next* honest poll is measured from the rejected value, not the old one.
    usage.apply(
        [_dev(rx=27_283_741_946)], link_max_bytes_s=LINK, now=t1 + _dt.timedelta(seconds=60)
    )
    assert usage.totals()["downloaded"] == 10_000


def test_the_allowance_scales_with_real_elapsed_time(usage: WanUsage) -> None:
    """A poll after a long outage legitimately carries a large delta and must not be rejected."""
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    a_day_later = _T0 + _dt.timedelta(days=1)
    result = usage.apply([_dev(rx=50_000_000_000)], link_max_bytes_s=LINK, now=a_day_later)
    assert result.rejected == 0
    assert usage.totals()["downloaded"] == 50_000_000_000


def test_a_departed_device_drops_its_cursor(usage: WanUsage) -> None:
    """Its counter restarts when it returns; a stale cursor would read as one huge delta."""
    usage.apply([_dev(mac="AA:BB", rx=1_000), _dev(mac="CC:DD", rx=8_000)],
                link_max_bytes_s=LINK, now=_T0)
    t1 = _T0 + _dt.timedelta(seconds=60)
    usage.apply([_dev(mac="AA:BB", rx=1_000)], link_max_bytes_s=LINK, now=t1)  # CC:DD left
    t2 = t1 + _dt.timedelta(seconds=60)
    usage.apply(
        [_dev(mac="AA:BB", rx=1_000), _dev(mac="CC:DD", rx=300)], link_max_bytes_s=LINK, now=t2
    )
    by_mac = {d.mac: d for d in usage.by_device()}
    assert by_mac["CC:DD"].rx == 300  # counted fresh, not 300 - 8000


def test_daily_totals_accumulate_rather_than_overwrite(usage: WanUsage) -> None:
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    for i in range(1, 4):
        usage.apply(
            [_dev(rx=1_000 * i)], link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(seconds=60 * i)
        )
    assert usage.totals()["downloaded"] == 3_000
    assert usage.daily_series() == [("2026-08-29", 3_000, 0)]


def test_usage_is_split_across_day_boundaries(usage: WanUsage) -> None:
    before = _dt.datetime(2026, 8, 29, 23, 59, tzinfo=_dt.UTC)
    after = _dt.datetime(2026, 8, 30, 0, 1, tzinfo=_dt.UTC)
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=before)
    usage.apply([_dev(rx=1_000)], link_max_bytes_s=LINK, now=before)
    usage.apply([_dev(rx=3_000)], link_max_bytes_s=LINK, now=after)
    assert usage.daily_series() == [("2026-08-29", 1_000, 0), ("2026-08-30", 2_000, 0)]


def test_by_device_uses_the_most_recent_name(usage: WanUsage) -> None:
    """A renamed device should read as its current name, not whatever it was called first."""
    usage.apply([_dev(host="old", rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(host="old", rx=1_000)], link_max_bytes_s=LINK, now=_T0)
    usage.apply(
        [_dev(host="new", rx=2_000)], link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(days=1)
    )
    rows = usage.by_device()
    assert len(rows) == 1
    assert rows[0].host == "new"
    assert rows[0].rx == 2_000


def test_range_bounds_accept_iso_timestamps(usage: WanUsage) -> None:
    """The ledger's routes pass ISO bounds; both stores must accept the same ones."""
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(rx=1_000)], link_max_bytes_s=LINK, now=_T0)
    assert usage.totals(start="2026-08-29T00:00:00+00:00")["downloaded"] == 1_000
    assert usage.totals(start="2026-08-30T00:00:00+00:00")["downloaded"] == 0


def test_an_empty_poll_is_harmless(usage: WanUsage) -> None:
    assert usage.apply([], link_max_bytes_s=LINK, now=_T0).counted == 0


def test_devices_count_is_distinct_macs(usage: WanUsage) -> None:
    usage.apply([_dev(mac="AA:BB", rx=0), _dev(mac="CC:DD", rx=0)],
                link_max_bytes_s=LINK, now=_T0)
    t1 = _T0 + _dt.timedelta(seconds=60)
    usage.apply([_dev(mac="AA:BB", rx=10), _dev(mac="CC:DD", rx=20)],
                link_max_bytes_s=LINK, now=t1)
    assert usage.totals()["devices"] == 2


# --- the source ---------------------------------------------------------------------------


def _monitor(usage, *, meta=None, **router_kw):
    from orchestrator.config import MonitoringConfig, RouterSource
    from orchestrator.monitor.wan import WanDeps, WanMonitor

    router_kw.setdefault("enabled", True)
    router_kw.setdefault("password", "pw")
    mon = MonitoringConfig(router=RouterSource(**router_kw))
    store = dict(meta or {})
    return WanMonitor(mon, WanDeps(usage=usage, get_meta=store.get))


def test_a_paused_source_makes_no_network_call(usage: WanUsage) -> None:
    """The entire point of pausing is to release the router's single admin session, so a paused
    poll must not touch the client at all — not merely discard its result."""
    from orchestrator.monitor.wan import PAUSE_KEY

    future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=15)).isoformat()
    mon = _monitor(usage, meta={PAUSE_KEY: future})
    calls = []
    mon.client.devices = lambda: calls.append(1)  # type: ignore[union-attr]

    samples, evals, snap = mon.collect()
    assert calls == []
    assert snap.paused is True
    assert samples == [] and evals == []


def test_a_paused_source_is_not_reported_as_unreachable(usage: WanUsage) -> None:
    """ok stays True while paused: the router is presumably fine, and saying "unreachable" would
    send someone debugging a network that is not broken."""
    from orchestrator.monitor.wan import PAUSE_KEY

    future = (_dt.datetime.now(_dt.UTC) + _dt.timedelta(minutes=15)).isoformat()
    _samples, _evals, data = _monitor(usage, meta={PAUSE_KEY: future}).collect()
    assert data is not None  # the worker turns "data is not None" into ok=True


def test_an_expired_pause_polls_again(usage: WanUsage) -> None:
    from orchestrator.monitor.wan import PAUSE_KEY

    past = (_dt.datetime.now(_dt.UTC) - _dt.timedelta(minutes=1)).isoformat()
    mon = _monitor(usage, meta={PAUSE_KEY: past}, manage_settings=False)
    mon.client.devices = lambda: [_dev(rx=1)]  # type: ignore[union-attr]
    _samples, _evals, snap = mon.collect()
    assert snap.paused is False
    assert len(snap.devices) == 1


def test_a_corrupt_pause_value_does_not_wedge_polling(usage: WanUsage) -> None:
    from orchestrator.monitor.wan import PAUSE_KEY

    mon = _monitor(usage, meta={PAUSE_KEY: "not-a-timestamp"}, manage_settings=False)
    mon.client.devices = lambda: [_dev(rx=1)]  # type: ignore[union-attr]
    _samples, _evals, snap = mon.collect()
    assert snap.paused is False


def test_an_unreachable_router_yields_no_snapshot(usage: WanUsage) -> None:
    """None -> ok=False, so the UI says unreachable instead of showing stale numbers as live."""
    mon = _monitor(usage, manage_settings=False)
    mon.client.devices = lambda: None  # type: ignore[union-attr]
    _samples, _evals, data = mon.collect()
    assert data is None


def test_samples_are_aggregate_not_per_device(usage: WanUsage) -> None:
    """A sample per device per minute would scale this table with the size of the household;
    per-device history lives in wan_usage instead."""
    mon = _monitor(usage, manage_settings=False)
    mon.client.devices = lambda: [  # type: ignore[union-attr]
        DeviceStats("AA:BB", "a", "wired", 0, 0, curr_rx=10, curr_tx=1),
        DeviceStats("CC:DD", "b", "5g", 0, 0, curr_rx=5, curr_tx=2),
    ]
    samples, _evals, _snap = mon.collect()
    assert [s.name for s in samples] == ["wan.rx", "wan.tx"]
    assert samples[0].value == 15.0 and samples[1].value == 3.0


def test_device_aliases_rename_the_snapshot(usage: WanUsage) -> None:
    """Phones randomise their MAC, so the reported hostname is often unhelpful."""
    mon = _monitor(usage, manage_settings=False, device_aliases={"AA:BB": "Youssef's phone"})
    mon.client.devices = lambda: [_dev(mac="AA:BB", host="android-1234")]  # type: ignore[union-attr]
    _samples, _evals, snap = mon.collect()
    assert snap.devices[0].host == "Youssef's phone"


def test_settings_are_checked_once_per_process(usage: WanUsage) -> None:
    """These writes land in the router's flash — checking every poll would be wasteful even
    when it is a no-op read."""
    mon = _monitor(usage, manage_settings=True)
    checks = []
    mon.client.ensure_stats_enabled = lambda: (checks.append(1), True)[1]  # type: ignore[union-attr]
    mon.client.devices = lambda: [_dev(rx=1)]  # type: ignore[union-attr]
    for _ in range(3):
        mon.collect()
    assert len(checks) == 1
