from __future__ import annotations

import datetime as _dt
from pathlib import Path

from mediacore.audit import open_db
from orchestrator.ledger import Ledger
from orchestrator.util import human_bytes, parse_bytes, parse_range


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
