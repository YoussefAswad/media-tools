from __future__ import annotations

import datetime as _dt
from pathlib import Path

from mediacore.audit import open_db
from orchestrator.ledger import Ledger
from orchestrator.util import human_bytes, parse_bytes, parse_range, range_bounds


def mk(tmp_path: Path) -> Ledger:
    return Ledger(open_db(tmp_path))


def test_record_auto_upserts_by_infohash(tmp_path):
    lg = mk(tmp_path)
    lg.record_auto(infohash="AABB", name="R1", kind="movie", app="radarr",
                   downloaded_bytes=100, uploaded_bytes=10, size_bytes=90, completion_on=1700000000)
    # same torrent re-imported (upgrade) -> update, not duplicate; hash case-insensitive
    lg.record_auto(infohash="aabb", name="R1", kind="movie", app="radarr",
                   downloaded_bytes=150, uploaded_bytes=40, size_bytes=90, completion_on=1700000000)
    rows = lg.list()
    assert len(rows) == 1
    assert rows[0].downloaded_bytes == 150 and rows[0].uploaded_bytes == 40
    assert rows[0].total_bytes == 190


def test_edit_marks_edited_and_blocks_reimport(tmp_path):
    lg = mk(tmp_path)
    lg.record_auto(infohash="ff01", name="R", kind="episode", app="sonarr",
                   downloaded_bytes=100, uploaded_bytes=5)
    row = lg.list()[0]
    assert lg.update(row.id, downloaded_bytes=999, kind="show") is True
    # a later auto capture for the same hash must NOT clobber the hand-edited row
    lg.record_auto(infohash="ff01", name="R", kind="episode", app="sonarr",
                   downloaded_bytes=222, uploaded_bytes=50)
    r = lg.get(row.id)
    assert r.downloaded_bytes == 999 and r.kind == "show" and r.edited == 1


def test_add_manual_and_delete(tmp_path):
    lg = mk(tmp_path)
    rid = lg.add_manual(name="Elden Ring", kind="game", downloaded_bytes=parse_bytes("50 GB"),
                        uploaded_bytes=0, notes="steam")
    rows = lg.list()
    assert len(rows) == 1 and rows[0].source == "manual" and rows[0].kind == "game"
    assert lg.delete(rid) is True and lg.list() == []


def test_range_and_filters(tmp_path):
    lg = mk(tmp_path)
    now = _dt.datetime.now(_dt.UTC)
    old = (now - _dt.timedelta(days=100)).isoformat()
    recent = (now - _dt.timedelta(days=2)).isoformat()
    lg.add_manual(name="old", kind="movie", downloaded_bytes=10, uploaded_bytes=0, completed_at=old)
    lg.add_manual(name="new", kind="game", downloaded_bytes=20, uploaded_bytes=5, completed_at=recent)

    start, end = parse_range("30d")
    rows = lg.list(start=start, end=end)
    assert [r.name for r in rows] == ["new"]
    assert lg.totals(start=start, end=end)["total"] == 25
    # all-time sees both
    s2, e2 = parse_range("all")
    assert lg.totals(start=s2, end=e2)["count"] == 2
    # type filter
    assert [r.name for r in lg.list(kind="game")] == ["new"]
    # source filter
    assert len(lg.list(source="manual")) == 2


def test_daily_series(tmp_path):
    lg = mk(tmp_path)
    day = "2026-08-20T12:00:00+00:00"
    lg.add_manual(name="a", kind="movie", downloaded_bytes=10, uploaded_bytes=1, completed_at=day)
    lg.add_manual(name="b", kind="movie", downloaded_bytes=20, uploaded_bytes=2, completed_at=day)
    series = lg.daily_series()
    assert ("2026-08-20", 30, 3) in series


def test_human_and_parse_bytes():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.50 KB"
    assert human_bytes(1024**3) == "1.00 GB"
    assert parse_bytes("12.3 GB") == int(12.3 * 1024**3)
    assert parse_bytes("500MB") == 500 * 1024**2
    assert parse_bytes("1048576") == 1048576
    assert parse_bytes("") == 0 and parse_bytes(None) == 0


def test_human_bytes_iec_suffix():
    # same 1024-based magnitude, only the suffix changes to the IEC form
    assert human_bytes(1536, iec=True) == "1.50 KiB"
    assert human_bytes(1024**3, iec=True) == "1.00 GiB"
    assert human_bytes(0, iec=True) == "0 B"


def test_parse_bytes_accepts_iec_units():
    assert parse_bytes("1 GiB") == 1024**3
    assert parse_bytes("500MiB") == 500 * 1024**2
    # GB and GiB parse to the same bytes (both 1024-based)
    assert parse_bytes("2 GB") == parse_bytes("2 GiB")


def test_range_bounds_custom_dates_override_preset():
    # explicit dates win over the preset key
    lo, hi = range_bounds("30d", "2026-01-01", "2026-01-31")
    assert lo == "2026-01-01T00:00:00+00:00" and hi == "2026-01-31T23:59:59+00:00"
    # lone start -> open-ended
    lo, hi = range_bounds("30d", "2026-01-01", None)
    assert lo == "2026-01-01T00:00:00+00:00" and hi is None
    # invalid date ignored -> falls back to preset
    lo, hi = range_bounds("all", "nonsense", "")
    assert lo is None  # 'all' preset
    # no dates -> preset applies (30d has a start)
    lo, hi = range_bounds("30d", None, None)
    assert lo is not None and hi is not None


def test_cutoffs_crud_and_latest(tmp_path):
    from mediacore.audit import open_db
    from orchestrator.ledger import Cutoffs
    cf = Cutoffs(open_db(tmp_path))
    assert cf.latest() is None and cf.latest_date() is None
    a = cf.add(cutoff_date="2026-08-01", limit_bytes=1024**4, note="Aug")
    cf.add(cutoff_date="2026-09-01", limit_bytes=0, note="Sep")
    latest = cf.latest()
    assert latest.cutoff_date == "2026-09-01"  # most recent
    assert cf.latest_date() == "2026-09-01"
    assert len(cf.list()) == 2
    assert cf.update(a, cutoff_date="2026-08-02", limit_bytes=2 * 1024**4, note="Aug2") is True
    got = next(c for c in cf.list() if c.id == a)
    assert got.cutoff_date == "2026-08-02" and got.limit_bytes == 2 * 1024**4
    assert cf.delete(a) is True and len(cf.list()) == 1


def test_kind_icon():
    from orchestrator.util import kind_icon
    assert kind_icon("movie") == "🎬"
    assert kind_icon("episode") == "📺" and kind_icon("show") == "📺"
    assert kind_icon("game") == "🎮" and kind_icon("software") == "💿"
    assert kind_icon(None) == "📦" and kind_icon("weird") == "📦"
