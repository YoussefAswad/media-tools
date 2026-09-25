"""The WAN delta engine: turning the router's cumulative counters into a durable daily record.

Every rule here exists because of a real observed case on the Archer VR600, so the tests are
written against those cases rather than against the implementation.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from mediacore.db import open_db
from mediacore.router import DeviceStats
from orchestrator.wan_usage import WanUsage, display_name, fold_identities, reconcile

# The real line rate of the host this was built for (~30 Mbit VDSL, measured 3.78 MB/s). The
# suite runs at the true bound on purpose: a generously-set one is exactly what let a bogus
# counter through in production, so tests written against a loose bound test nothing.
LINK = 4_000_000  # bytes/sec ceiling used for the plausibility bound

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


def test_a_never_seen_mac_baselines_and_counts_nothing(usage: WanUsage) -> None:
    """This rule used to read the other way — "a device appearing is new to the router too, so its
    counter is genuinely new traffic". It is false. The router keeps a device's counter while the
    device is missing from its host table, so a first sighting can carry an arbitrary amount of
    history, and crediting it re-counts that history on every reappearance."""
    usage.apply([_dev(mac="AA:BB", rx=100)], link_max_bytes_s=LINK, now=_T0)
    later = _T0 + _dt.timedelta(seconds=60)
    result = usage.apply(
        [_dev(mac="AA:BB", rx=100), _dev(mac="CC:DD", host="phone", rx=5_000)],
        link_max_bytes_s=LINK,
        now=later,
    )
    assert result.baselined == 1
    assert usage.by_device() == []  # nothing counted at all, so no row exists yet

    # And the poll after it is measured from that baseline, so nothing is lost but the first gap.
    usage.apply(
        [_dev(mac="CC:DD", host="phone", rx=5_400)],
        link_max_bytes_s=LINK,
        now=later + _dt.timedelta(seconds=60),
    )
    assert usage.by_device()[0].rx == 400


def test_a_counter_going_backwards_is_a_reset_not_a_wrap(usage: WanUsage) -> None:
    """A 32-bit wrap needs 4.29 GB; at any plausible link speed that cannot happen inside one
    interval, so backwards means the router restarted the counter.

    It re-bases and counts *nothing*. The post-reset value is not a measurement of anything this
    engine observed — and leaning on the plausibility bound to cap it does not work, because that
    bound scales with the gap since the device was last seen and so evaporates in exactly the case
    where the value is least trustworthy.
    """
    usage.apply([_dev(rx=5_000_000)], link_max_bytes_s=LINK, now=_T0)
    t1 = _T0 + _dt.timedelta(seconds=60)
    result = usage.apply([_dev(rx=42_000)], link_max_bytes_s=LINK, now=t1)
    assert result.resets == 1
    assert usage.totals()["downloaded"] == 0  # not a negative, not 4 GiB + delta, not 42_000

    # Re-based, so the next poll is differenced against the restarted counter.
    usage.apply([_dev(rx=52_000)], link_max_bytes_s=LINK, now=t1 + _dt.timedelta(seconds=60))
    assert usage.totals()["downloaded"] == 10_000


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


def test_a_departed_device_keeps_its_cursor(usage: WanUsage) -> None:
    """The router keeps the device's counter while it is off the host table, so the cursor has to
    survive the absence or the return is differenced against nothing.

    Measured directly: the same ``rx=3618567324`` was offered once a minute for 23 hours by a
    device that flapped in and out. The counter never moved — it was our cursor that kept being
    thrown away.
    """
    usage.apply([_dev(mac="AA:BB", rx=1_000), _dev(mac="CC:DD", rx=8_000)],
                link_max_bytes_s=LINK, now=_T0)
    t1 = _T0 + _dt.timedelta(seconds=60)
    usage.apply([_dev(mac="AA:BB", rx=1_000)], link_max_bytes_s=LINK, now=t1)  # CC:DD left
    t2 = t1 + _dt.timedelta(seconds=60)
    usage.apply(
        [_dev(mac="AA:BB", rx=1_000), _dev(mac="CC:DD", rx=8_300)], link_max_bytes_s=LINK, now=t2
    )
    by_mac = {d.macs[0]: d for d in usage.by_device()}
    assert by_mac["CC:DD"].rx == 300  # differenced against the retained cursor, not re-counted


def test_a_device_that_flaps_out_of_the_host_table_is_not_re_counted(usage: WanUsage) -> None:
    """The production bug, in miniature.

    A device present on one poll and absent on the next had its cursor deleted, so every
    reappearance offered its whole cumulative counter as fresh traffic. Recorded: 1,249 GB in a
    single day for one device on a link whose physical ceiling is ~326 GB/day, while its cursor
    sat at 0.29 GB — the signature of re-baselining, not of traffic.
    """
    counter = 1_000_000_000  # a device that has already moved a GB before we ever look
    now = _T0
    usage.apply([_dev(mac="CC:DD", rx=counter)], link_max_bytes_s=LINK, now=now)
    usage.apply([_dev(mac="AA:BB", rx=0)], link_max_bytes_s=LINK, now=now)  # cursor now exists

    for _ in range(10):
        counter += 100_000
        now += _dt.timedelta(seconds=60)
        usage.apply([_dev(mac="CC:DD", rx=counter)], link_max_bytes_s=LINK, now=now)
        now += _dt.timedelta(seconds=60)
        usage.apply([], link_max_bytes_s=LINK, now=now)  # gone from the host table

    by_mac = {d.macs[0]: d for d in usage.by_device()}
    assert by_mac["CC:DD"].rx == 1_000_000  # ten honest increments, not ten whole counters


def test_a_cursor_survives_a_missed_poll(usage: WanUsage) -> None:
    """The mechanism the flapping case depends on, asserted where it lives."""
    usage.apply([_dev(mac="CC:DD", rx=8_000)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([], link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(seconds=60))
    rows = usage.db.execute("SELECT mac, rx FROM wan_counters").fetchall()
    assert [(r["mac"], r["rx"]) for r in rows] == [("CC:DD", 8_000)]


def test_a_cursor_is_expired_once_the_device_has_been_gone_too_long(usage: WanUsage) -> None:
    """Retention is not forever: past the horizon the old value is too stale to difference
    against (the counter may have wrapped), so the return is baselined instead."""
    usage.apply([_dev(mac="AA:BB", rx=0), _dev(mac="CC:DD", rx=8_000)],
                link_max_bytes_s=LINK, now=_T0)
    gone = _T0 + _dt.timedelta(days=8)
    result = usage.apply([_dev(mac="AA:BB", rx=0)], link_max_bytes_s=LINK, now=gone)
    assert result.expired == 1

    back = gone + _dt.timedelta(seconds=60)
    result = usage.apply([_dev(mac="CC:DD", rx=9_000)], link_max_bytes_s=LINK, now=back)
    assert result.baselined == 1
    assert usage.totals()["downloaded"] == 0  # baselined, not counted as 9_000 and not as 1_000


def test_expiry_never_touches_the_daily_record(usage: WanUsage) -> None:
    """wan_usage is the durable history and is never pruned; only the cursor ages out."""
    usage.apply([_dev(mac="CC:DD", rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(mac="CC:DD", rx=5_000)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(mac="AA:BB", rx=0)], link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(days=8))
    assert usage.totals()["downloaded"] == 5_000


def test_two_rows_for_one_mac_in_a_poll_are_not_double_counted(usage: WanUsage) -> None:
    """The same counter reported twice is one counter: differencing both copies against the same
    stored value would add the delta to the day twice."""
    usage.apply([_dev(mac="CC:DD", rx=1_000)], link_max_bytes_s=LINK, now=_T0)
    result = usage.apply(
        [_dev(mac="CC:DD", rx=3_000), _dev(mac="CC:DD", rx=3_000)],
        link_max_bytes_s=LINK,
        now=_T0 + _dt.timedelta(seconds=60),
    )
    assert usage.totals()["downloaded"] == 2_000
    assert result.resets == 0 and result.duplicates == 0


def _duplicated_polls(usage: WanUsage, *, stale: int, live: list[int], stale_first: bool) -> None:
    """One MAC reported twice per poll: a frozen entry at ``stale`` beside a live counter."""
    now = _T0
    usage.apply([_dev(mac="98:59", host="logy", rx=stale)], link_max_bytes_s=LINK, now=now)
    for value in live:
        now += _dt.timedelta(seconds=60)
        frozen = _dev(mac="98:59", host="Unknown", rx=stale)
        current = _dev(mac="98:59", host="logy", rx=value)
        rows = [frozen, current] if stale_first else [current, frozen]
        usage.apply(rows, link_max_bytes_s=LINK, now=now)


@pytest.mark.parametrize("stale_first", [True, False])
def test_a_frozen_duplicate_row_above_the_live_counter_fabricates_nothing(
    usage: WanUsage, stale_first: bool
) -> None:
    """The production bug of 13-18 Sep, in miniature.

    The router kept logy's old host-table entry frozen at rx=199,743,377 beside the live entry
    it had restarted from 12 MB. One cursor per MAC flipped between the two, so every poll
    counted ``stale - live`` (90-190 MB, under the per-poll bound) as traffic: 427 GB over two
    days, when the independent metrics counter put the whole household at 66 GB. Only the live
    counter's own growth is real.
    """
    live = [12_444_194 + 1_000_000 * i for i in range(30)]
    _duplicated_polls(usage, stale=199_743_377, live=live, stale_first=stale_first)
    # The first live value has nothing to continue from (the only stored counter is above it),
    # so it is a restart and counts nothing; every poll after it counts the live growth.
    assert usage.totals()["downloaded"] == live[-1] - live[0]


@pytest.mark.parametrize("stale_first", [True, False])
def test_a_frozen_duplicate_row_below_the_live_counter_fabricates_nothing(
    usage: WanUsage, stale_first: bool
) -> None:
    live = [5_000_000 + 100_000 * i for i in range(30)]
    _duplicated_polls(usage, stale=1_000_000, live=live, stale_first=stale_first)
    # With the frozen row *below*, the live row could also continue it — but the frozen row is
    # the closer match (delta 0), so the live row starts a new series rather than claiming 4 MB.
    assert usage.totals()["downloaded"] == live[-1] - live[0]


def test_the_duplicate_disappearing_counts_only_the_live_growth(usage: WanUsage) -> None:
    live = [12_000_000, 13_000_000, 14_000_000]
    _duplicated_polls(usage, stale=199_743_377, live=live, stale_first=True)
    usage.apply(
        [_dev(mac="98:59", host="logy", rx=14_500_000)],
        link_max_bytes_s=LINK,
        now=_T0 + _dt.timedelta(minutes=10),
    )
    assert usage.totals()["downloaded"] == 2_500_000
    rows = usage.db.execute("SELECT rx FROM wan_counters WHERE mac='98:59'").fetchall()
    assert [r["rx"] for r in rows] == [14_500_000]  # the frozen counter is forgotten


def test_a_replay_of_the_logged_logy_sequence(usage: WanUsage) -> None:
    """The values the production log actually printed, ~one poll a minute for 36 hours: a stale
    199,743,377 beside a live counter that climbed from 12.4 MB and stalled at 111.4 MB."""
    stale = 199_743_377
    climb = [12_444_194, 35_935_585, 51_127_819, 58_942_571, 63_700_564, 64_908_851,
             65_080_369, 75_557_495, 79_651_427, 81_835_379, 85_336_795, 95_848_657,
             105_035_122, 110_266_355, 111_287_624, 111_364_221]
    live = climb + [111_364_221] * 1_880
    _duplicated_polls(usage, stale=stale, live=live, stale_first=True)
    assert usage.totals()["downloaded"] == 111_364_221 - 12_444_194  # ~99 MB, not ~170 GB


def test_a_duplicate_is_reported_in_the_poll_result(usage: WanUsage) -> None:
    usage.apply([_dev(mac="98:59", rx=100)], link_max_bytes_s=LINK, now=_T0)
    result = usage.apply(
        [_dev(mac="98:59", rx=100), _dev(mac="98:59", rx=5)],
        link_max_bytes_s=LINK,
        now=_T0 + _dt.timedelta(seconds=60),
    )
    assert result.duplicates == 1
    assert result.devices == 1  # devices are MACs, not rows
    assert result.resets == 1  # the new, lower series; it counts nothing


def test_a_legacy_cursor_table_is_migrated_not_dropped(tmp_path) -> None:
    """Dropping the old one-per-MAC table would re-baseline every device, and cost an absent
    device the retained cursor its return is differenced against."""
    db = open_db(tmp_path)
    db.executescript(
        "CREATE TABLE wan_cursor (mac TEXT PRIMARY KEY, rx INTEGER NOT NULL DEFAULT 0, "
        "tx INTEGER NOT NULL DEFAULT 0, seen_at TEXT NOT NULL DEFAULT '');"
    )
    db.execute(
        "INSERT INTO wan_cursor VALUES ('CC:DD', 8000, 100, ?)",
        (_T0.isoformat(timespec="seconds"),),
    )
    db.commit()
    usage = WanUsage(db)
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='wan_cursor'"
    ).fetchone() is None
    result = usage.apply(
        [_dev(mac="CC:DD", rx=8_300, tx=100)],
        link_max_bytes_s=LINK,
        now=_T0 + _dt.timedelta(seconds=60),
    )
    assert result.cold_start is False
    assert usage.totals()["downloaded"] == 300
    WanUsage(db)  # idempotent: a second start finds nothing to migrate


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
    assert rows[0].macs == ("AA:BB",)
    assert rows[0].rx == 2_000


def test_a_mac_that_reported_no_hostname_keeps_its_named_row(usage: WanUsage) -> None:
    """The router stores the raw MAC as the hostname while it is re-creating a host-table entry.
    If that day wins the name, one device splits into two identities — one named, one a bare MAC —
    and the table shows the same phone twice."""
    usage.apply([_dev(mac="CC:DD", host="logy", rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(mac="CC:DD", host="logy", rx=1_000)], link_max_bytes_s=LINK, now=_T0)
    later = _T0 + _dt.timedelta(days=1)
    usage.apply([_dev(mac="CC:DD", host="CC:DD", rx=1_000)], link_max_bytes_s=LINK, now=later)
    usage.apply([_dev(mac="CC:DD", host="CC:DD", rx=2_000)], link_max_bytes_s=LINK, now=later)
    rows = usage.by_device()
    assert len(rows) == 1
    assert rows[0].host == "logy"  # the named day wins even though it is older
    assert rows[0].rx == 2_000


def test_by_device_groups_a_randomised_mac_under_one_name(usage: WanUsage) -> None:
    """Measured: one Xiaomi TV box reached the router under three locally-administered MACs and
    filled three rows of the table. Storage stays per-MAC — that is the raw fact — and identity is
    resolved on the way out."""
    macs = ["0A:39", "B6:A2", "A2:1E"]
    usage.apply([_dev(mac=m, host="Xiaomi-TV-Box", rx=0) for m in macs],
                link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(mac=m, host="Xiaomi-TV-Box", rx=1_000) for m in macs],
                link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(seconds=60))
    assert len(usage.by_device()) == 3
    folded = fold_identities(usage.by_device())
    assert len(folded) == 1
    assert folded[0].host == "Xiaomi-TV-Box"
    assert folded[0].rx == 3_000
    assert sorted(folded[0].macs) == sorted(macs)


def test_an_alias_groups_macs_the_router_names_differently(usage: WanUsage) -> None:
    """An alias is for the case the hostname cannot fix: one handset the router sees under two
    names. Resolved at read time, so adding it re-groups the history that is already stored."""
    usage.apply([_dev(mac="AA:BB", host="android-1234", rx=0),
                 _dev(mac="CC:DD", host="android-9999", rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(mac="AA:BB", host="android-1234", rx=1_000),
                 _dev(mac="CC:DD", host="android-9999", rx=500)],
                link_max_bytes_s=LINK, now=_T0 + _dt.timedelta(seconds=60))
    folded = fold_identities(usage.by_device(), {"aa:bb": "phone", "CC:DD": "phone"})
    assert len(folded) == 1
    assert folded[0].host == "phone" and folded[0].rx == 1_500


def test_the_device_count_counts_identities_not_macs(usage: WanUsage) -> None:
    """The tile and the table are answers to the same question and must not disagree."""
    usage.apply([_dev(mac="AA:BB", host="laptop", rx=0),
                 _dev(mac="CC:DD", host="Xiaomi-TV-Box", rx=0),
                 _dev(mac="EE:FF", host="Xiaomi-TV-Box", rx=0)], link_max_bytes_s=LINK, now=_T0)
    t1 = _T0 + _dt.timedelta(seconds=60)
    usage.apply([_dev(mac="AA:BB", host="laptop", rx=10),
                 _dev(mac="CC:DD", host="Xiaomi-TV-Box", rx=20),
                 _dev(mac="EE:FF", host="Xiaomi-TV-Box", rx=30)], link_max_bytes_s=LINK, now=t1)
    assert len(usage.by_device()) == 3
    assert len(fold_identities(usage.by_device())) == 2


def test_display_name_prefers_an_alias_then_the_reported_name(usage: WanUsage) -> None:
    """Aliases are hand-written YAML while DeviceStats.mac is upper-cased, so the lookup has to
    tolerate either casing."""
    assert display_name({"AA:BB": "phone"}, "AA:BB", "android-1") == "phone"
    assert display_name({"aa:bb": "phone"}, "AA:BB", "android-1") == "phone"
    assert display_name({}, "AA:BB", "android-1") == "android-1"
    assert display_name(None, "AA:BB", "") == "AA:BB"


def test_range_bounds_accept_iso_timestamps(usage: WanUsage) -> None:
    """The ledger's routes pass ISO bounds; both stores must accept the same ones."""
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(rx=1_000)], link_max_bytes_s=LINK, now=_T0)
    assert usage.totals(start="2026-08-29T00:00:00+00:00")["downloaded"] == 1_000
    assert usage.totals(start="2026-08-30T00:00:00+00:00")["downloaded"] == 0


def test_an_empty_poll_is_harmless(usage: WanUsage) -> None:
    assert usage.apply([], link_max_bytes_s=LINK, now=_T0).counted == 0


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


def test_an_exclusive_end_instant_does_not_include_its_own_day(usage: WanUsage) -> None:
    """One period ends exactly where the next begins; the boundary day must not land in both.

    Truncating an exclusive end instant to a date made it inclusive, so a period starting
    2026-09-11T00:00 and its predecessor ending there overlapped by a full day. Each window
    still reconciled on its own, so nothing flagged it — the day was just counted twice.
    """
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(rx=1_000_000)], link_max_bytes_s=LINK,
                now=_dt.datetime(2026, 9, 11, 6, 0, tzinfo=_dt.UTC))

    before = usage.totals(start=None, end="2026-09-11T00:00:00+00:00")
    after = usage.totals(start="2026-09-11T00:00:00+00:00", end=None)
    assert before["total"] == 0          # 11 Sep belongs to the later window only
    assert after["total"] == 1_000_000
    assert before["total"] + after["total"] == usage.totals()["total"]


def test_a_bare_date_end_still_names_that_whole_day(usage: WanUsage) -> None:
    """`totals(start=day, end=day)` asks for one day, not for the day before it."""
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(rx=5_000)], link_max_bytes_s=LINK,
                now=_dt.datetime(2026, 8, 29, 23, 0, tzinfo=_dt.UTC))
    assert usage.totals(start="2026-08-29", end="2026-08-29")["total"] == 5_000


def test_an_inclusive_end_of_day_bound_is_unchanged(usage: WanUsage) -> None:
    """The UI's custom date range ends at 23:59:59, which must still mean that day."""
    usage.apply([_dev(rx=0)], link_max_bytes_s=LINK, now=_T0)
    usage.apply([_dev(rx=7_000)], link_max_bytes_s=LINK,
                now=_dt.datetime(2026, 8, 29, 12, 0, tzinfo=_dt.UTC))
    assert usage.totals(end="2026-08-29T23:59:59+00:00")["total"] == 7_000


# --- reconciliation against the router's sampled rate -------------------------------------

_DAY = 1_440  # samples in a day at a 60 s poll


def test_a_clean_day_reconciles() -> None:
    """Numbers from a real clean day (2026-09-16): 31.66 GB recorded, 31.60 GB sampled."""
    check = reconcile("2026-09-16", 31_660_000_000, 31_600_000_000 / 60, _DAY, 60)
    assert check is not None and not check.suspect


def test_a_poisoned_day_is_flagged() -> None:
    """2026-09-13: 355.6 GB recorded, 42.95 GB sampled — logy's duplicated row."""
    check = reconcile("2026-09-13", 355_600_000_000, 42_950_000_000 / 60, _DAY, 60)
    assert check is not None and check.suspect
    assert round(check.ratio, 2) == 8.28


def test_an_under_count_is_flagged_too() -> None:
    check = reconcile("2026-09-09", 52_820_000_000, 107_630_000_000 / 60, _DAY, 60)
    assert check is not None and check.suspect and check.ratio < 1


def test_a_day_with_a_sampling_gap_gets_no_verdict() -> None:
    """Paused or unreachable: metrics has a hole the delta engine differenced straight across."""
    assert reconcile("2026-09-20", 20_000_000_000, 10_000_000_000 / 60, _DAY // 2, 60) is None


def test_a_quiet_day_gets_no_verdict() -> None:
    assert reconcile("2026-09-20", 300_000_000, 100_000_000 / 60, _DAY, 60) is None
