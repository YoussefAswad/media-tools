from __future__ import annotations

import pytest
from mediacore.db import add_columns, open_db


def test_add_columns_only_adds_what_is_missing(tmp_path) -> None:
    db = open_db(tmp_path)
    db.execute("CREATE TABLE t (a TEXT)")

    added = add_columns(db, "t", {"a": "TEXT", "b": "INTEGER", "c": "TEXT"})
    assert added == ["b", "c"]  # "a" already existed and must not be re-added

    cols = {r[1] for r in db.execute("PRAGMA table_info(t)")}
    assert cols == {"a", "b", "c"}


def test_add_columns_is_idempotent_and_keeps_existing_rows(tmp_path) -> None:
    """The real call path: every startup runs this against a db that may be any age. A second
    run must be a no-op, and must never disturb the rows already there."""
    db = open_db(tmp_path)
    db.execute("CREATE TABLE t (a TEXT)")
    db.execute("INSERT INTO t (a) VALUES ('keep me')")

    spec = {"b": "INTEGER NOT NULL DEFAULT 0", "c": "TEXT"}
    assert add_columns(db, "t", spec) == ["b", "c"]
    assert add_columns(db, "t", spec) == []  # nothing left to do

    row = db.execute("SELECT a, b, c FROM t").fetchone()
    assert row[0] == "keep me"
    assert row[1] == 0  # the NOT NULL default backfills the pre-existing row
    assert row[2] is None


def test_a_not_null_column_without_a_default_only_fails_once_there_is_data() -> None:
    """The trap this encodes: SQLite accepts ``ADD COLUMN ... NOT NULL`` with no default on an
    EMPTY table and refuses it on a populated one. A column spec missing its default therefore
    passes on a fresh dev/CI database and fails on the production one, at startup. Every
    NOT NULL entry in the added-columns maps carries a DEFAULT for exactly this reason.
    """
    import sqlite3

    empty = sqlite3.connect(":memory:")
    empty.execute("CREATE TABLE t (a TEXT)")
    assert add_columns(empty, "t", {"b": "INTEGER NOT NULL"}) == ["b"]  # silently fine

    populated = sqlite3.connect(":memory:")
    populated.execute("CREATE TABLE t (a TEXT)")
    populated.execute("INSERT INTO t VALUES ('x')")
    with pytest.raises(sqlite3.OperationalError):
        add_columns(populated, "t", {"b": "INTEGER NOT NULL"})

    # with the default, both work
    populated.execute("CREATE TABLE u (a TEXT)")
    populated.execute("INSERT INTO u VALUES ('x')")
    assert add_columns(populated, "u", {"b": "INTEGER NOT NULL DEFAULT 0"}) == ["b"]


def test_every_added_column_map_defaults_its_not_null_columns() -> None:
    """Guards the rule above across every table at once, so a new entry can't reintroduce
    the dev-passes/production-fails bug."""
    from mediacore.audit import _ACTIONS_ADDED
    from orchestrator.ledger import (
        _DOWNLOADS_ADDED,
        _QUOTA_ALERTS_ADDED,
        _QUOTA_PERIODS_ADDED,
    )
    from orchestrator.library.index import _LIBRARY_ADDED
    from orchestrator.queue import _JOBS_ADDED

    for name, spec in {
        "actions": _ACTIONS_ADDED,
        "jobs": _JOBS_ADDED,
        "quota_periods": _QUOTA_PERIODS_ADDED,
        "quota_alerts": _QUOTA_ALERTS_ADDED,
        "downloads": _DOWNLOADS_ADDED,
        "library_files": _LIBRARY_ADDED,
    }.items():
        for col, decl in spec.items():
            if "NOT NULL" in decl.upper():
                assert "DEFAULT" in decl.upper(), f"{name}.{col} is NOT NULL without a DEFAULT"


def test_add_columns_works_without_a_row_factory() -> None:
    """It reads PRAGMA table_info positionally, so it does not depend on the caller having set
    sqlite3.Row (open_db does; a bare connection does not)."""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (a TEXT)")
    assert add_columns(db, "t", {"b": "TEXT"}) == ["b"]
