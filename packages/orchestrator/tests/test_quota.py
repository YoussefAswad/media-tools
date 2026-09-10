"""Attributing the household's WAN usage to quotas.

Every rule here was measured against this household's real data before it was written, so the
tests are named after the case that produced them rather than after the code path they cover.
The figures in the docstrings are the actual measurements — if one of them stops holding, the
rule it justified is the thing to re-examine.
"""

from __future__ import annotations

import pytest
from orchestrator.config import QuotasConfig
from orchestrator.ledger import DownloadRow
from orchestrator.quota import attribute
from orchestrator.wan_usage import DeviceUsage

GB = 1_000_000_000


def _dev(host: str, total: int, kind: str = "wired") -> DeviceUsage:
    return DeviceUsage(macs=(host,), host=host, kind=kind, rx=total, tx=0)


def _row(
    row_id: int = 1,
    name: str = "a download",
    total: int = GB,
    day: str = "2026-09-08",
    device: str | None = None,
    quota: str | None = None,
    kind: str = "movie",
) -> DownloadRow:
    return DownloadRow(
        id=row_id, source="manual", name=name, kind=kind, app=None, infohash=None,
        downloaded_bytes=total, uploaded_bytes=0, size_bytes=total,
        completed_at=f"{day}T12:00:00+00:00", recorded_at=f"{day}T12:00:00+00:00",
        notes=None, edited=0, device=device, quota=quota,
    )


@pytest.fixture
def cfg() -> QuotasConfig:
    """The household's real shape: a server and a gaming PC owned by one person, a shared pot."""
    return QuotasConfig(
        server_device="Linux-G15",
        quotas={"youssef": {"allocation": "300 GB"}, "household": {"shared": True}},
        people={"youssef": {}, "logain": {}},
        devices={"Linux-G15": "youssef", "PC-114": "youssef", "Logains-iPhone": "logain"},
    )


# --- the base case ------------------------------------------------------------------------


def test_a_device_counts_to_its_owners_quota(cfg: QuotasConfig) -> None:
    result = attribute([_dev("Linux-G15", 148 * GB), _dev("Logains-iPhone", 113 * GB)], [], cfg)
    assert result.get("youssef").used == 148 * GB
    assert result.get("logain").used == 113 * GB
    assert result.household == 261 * GB


def test_an_unowned_device_counts_to_the_shared_pot(cfg: QuotasConfig) -> None:
    """The Xiaomi TV box and the guest phones are the answer here, not a gap to be filled."""
    result = attribute([_dev("Xiaomi-TV-Box", 3 * GB)], [], cfg)
    assert result.get("household").used == 3 * GB
    assert [d.host for d in result.unassigned] == ["Xiaomi-TV-Box"]


def test_a_person_with_no_quota_named_is_their_own_quota(cfg: QuotasConfig) -> None:
    """The middle level of device -> person -> quota is free in the common case."""
    assert "logain" in cfg.resolved_quotas()
    assert cfg.quota_of_device("Logains-iPhone") == "logain"


def test_a_person_can_be_pointed_at_a_shared_pot() -> None:
    cfg = QuotasConfig(
        people={"joury": {"quota": "household"}}, devices={"JourysLaptop": "joury"}
    )
    result = attribute([_dev("JourysLaptop", 12 * GB)], [], cfg)
    assert result.get("household").used == 12 * GB


# --- reassignment -------------------------------------------------------------------------


def test_a_ledger_row_moves_its_bytes_to_another_quota(cfg: QuotasConfig) -> None:
    """The Deer Hunter: 3.19 GB pulled on the server for someone else in the house."""
    devices = [_dev("Linux-G15", 148 * GB)]
    rows = [_row(name="The Deer Hunter", total=3190 * GB // 1000, quota="household")]
    result = attribute(devices, rows, cfg)
    assert result.get("youssef").used == 148 * GB - 3_190_000_000
    assert result.get("household").used == 3_190_000_000
    assert result.reassignments[0].from_quota == "youssef"
    assert result.reassignments[0].to_quota == "household"


def test_a_row_with_no_device_comes_out_of_the_server(cfg: QuotasConfig) -> None:
    """NULL means derive it, which is why the 38 historical rows were left NULL."""
    result = attribute([_dev("Linux-G15", 10 * GB)], [_row(total=GB, quota="household")], cfg)
    assert result.reassignments[0].device == "Linux-G15"
    assert result.get("youssef").used == 9 * GB


def test_a_row_already_counting_where_it_lands_is_not_a_reassignment(cfg: QuotasConfig) -> None:
    rows = [_row(device="PC-114", quota="youssef")]
    result = attribute([_dev("PC-114", 10 * GB)], rows, cfg)
    assert result.reassignments == ()
    assert result.get("youssef").used == 10 * GB


def test_a_row_pointing_at_a_deleted_quota_moves_nothing(cfg: QuotasConfig) -> None:
    """A quota removed from config is not a reason to invent one back."""
    result = attribute([_dev("Linux-G15", 10 * GB)], [_row(quota="ghost")], cfg)
    assert result.reassignments == ()
    assert result.unknown_targets == ("ghost",)
    assert result.get("youssef").used == 10 * GB


# --- the three rails ----------------------------------------------------------------------


def test_a_days_carve_out_is_not_clamped_to_that_day(cfg: QuotasConfig) -> None:
    """The measurement that set the period rule.

    Nine [BO3] rows are stamped 8 Sep 2026 and total 54.94 GB while PC-114's WAN *that day* was
    31.86 GB. Over 5-8 Sep the same device moved 62.20 GB, so the period reconciles and nothing
    should be dropped. A day-level carve-out would have driven the device negative.
    """
    devices = [_dev("PC-114", 62_200_000_000)]
    rows = [
        _row(row_id=i, name=f"[BO3] map {i}", total=6_104_444_444, day="2026-09-08",
             device="PC-114", quota="household", kind="game")
        for i in range(9)
    ]
    result = attribute(devices, rows, cfg)
    assert result.dropped == 0
    assert all(not r.clamped for r in result.reassignments)
    assert result.get("household").used == pytest.approx(54_940_000_000, rel=1e-3)


def test_a_carve_out_larger_than_the_device_used_is_clamped_and_reported(
    cfg: QuotasConfig,
) -> None:
    """Rail 2: the shortfall is surfaced, never absorbed.

    Clamping quietly would keep the totals adding up while telling the wrong story about the
    device — the exact shape of failure the WAN delta engine exists to refuse.
    """
    devices = [_dev("PC-114", 10 * GB)]
    rows = [_row(device="PC-114", total=25 * GB, quota="household")]
    result = attribute(devices, rows, cfg)
    assert result.get("household").used == 10 * GB
    assert result.get("youssef").used == 0
    assert result.dropped == 15 * GB
    assert result.reassignments[0].clamped is True
    assert result.reassignments[0].shortfall == 15 * GB


def test_a_quota_can_never_be_driven_negative(cfg: QuotasConfig) -> None:
    """Several over-committed rows on one device still cannot take more than it consumed."""
    devices = [_dev("PC-114", 10 * GB)]
    rows = [_row(row_id=i, device="PC-114", total=8 * GB, quota="household") for i in range(3)]
    result = attribute(devices, rows, cfg)
    assert result.get("youssef").used == 0
    assert result.get("household").used == 10 * GB
    assert result.dropped == 14 * GB


def test_a_row_older_than_the_wan_record_moves_nothing(cfg: QuotasConfig) -> None:
    """Rail 3: recording began 29 Aug 2026 while the ledger reaches back to 23 Aug.

    Moving those bytes anyway would carve them out of the device's *later* days to satisfy a
    download that happened before anything was watching.
    """
    devices = [_dev("Linux-G15", 50 * GB)]
    rows = [_row(day="2026-08-26", total=40 * GB, quota="household")]
    result = attribute(devices, rows, cfg, wan_first_day="2026-08-29")
    assert result.reassignments == ()
    assert result.before_record == 40 * GB
    assert result.get("youssef").used == 50 * GB


def test_the_same_row_inside_the_record_does_move(cfg: QuotasConfig) -> None:
    """The pair to the test above — the rail must be the date, not the reassignment itself."""
    devices = [_dev("Linux-G15", 50 * GB)]
    rows = [_row(day="2026-08-30", total=40 * GB, quota="household")]
    result = attribute(devices, rows, cfg, wan_first_day="2026-08-29")
    assert result.get("youssef").used == 10 * GB
    assert result.before_record == 0


# --- the invariant ------------------------------------------------------------------------


def test_attribution_never_creates_or_destroys_bytes(cfg: QuotasConfig) -> None:
    """The one failure this design cannot tolerate is a total that looks reasonable and is wrong.

    Reassignments, clamps, unknown targets and pre-record rows all in play at once; the sum over
    quotas still has to equal what the router measured.
    """
    devices = [
        _dev("Linux-G15", 148 * GB), _dev("PC-114", 90 * GB),
        _dev("Logains-iPhone", 113 * GB), _dev("Xiaomi-TV-Box", 3 * GB),
    ]
    rows = [
        _row(row_id=1, total=3 * GB, quota="household"),
        _row(row_id=2, device="PC-114", total=500 * GB, quota="household"),  # clamped hard
        _row(row_id=3, total=9 * GB, quota="ghost"),                          # unknown target
        _row(row_id=4, day="2026-08-01", total=9 * GB, quota="household"),    # pre-record
        _row(row_id=5, device="Logains-iPhone", total=2 * GB, quota="youssef"),
    ]
    result = attribute(devices, rows, cfg, wan_first_day="2026-08-29")
    assert result.household == 354 * GB
    assert result.attributed == 354 * GB
    assert result.reconciles is True
    assert all(q.used >= 0 for q in result.quotas)


def test_an_empty_household_reconciles(cfg: QuotasConfig) -> None:
    result = attribute([], [], cfg)
    assert result.reconciles is True
    assert result.household == 0


# --- presentation rules the UI depends on -------------------------------------------------


def test_a_quota_with_no_allocation_has_no_percentage(cfg: QuotasConfig) -> None:
    """None is not zero: the household pot is tracked but unbounded, and a bar would lie."""
    result = attribute([_dev("Xiaomi-TV-Box", 3 * GB)], [], cfg)
    household = result.get("household")
    assert household.allocation == 0
    assert household.pct is None
    assert household.tone == ""


@pytest.mark.parametrize(
    ("used", "tone"),
    [(100 * GB, "ok"), (260 * GB, "warn"), (301 * GB, "over")],
)
def test_tone_follows_the_allocation(cfg: QuotasConfig, used: int, tone: str) -> None:
    result = attribute([_dev("Linux-G15", used)], [], cfg)
    assert result.get("youssef").tone == tone


def test_a_quota_with_no_usage_still_appears(cfg: QuotasConfig) -> None:
    """'Logain used nothing this period' is an answer; a missing row is not."""
    result = attribute([_dev("Linux-G15", GB)], [], cfg)
    assert result.get("logain") is not None
    assert result.get("logain").used == 0
