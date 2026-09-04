from __future__ import annotations

from pathlib import Path

import pytest
from mediacore import notify as notify_mod
from mediacore.arr.events import parse_event
from mediacore.audit import OK, ActionRecord, Audit
from mediacore.config import parse_config
from mediacore.db import open_db
from mediacore.models import ArrInstance, CoreConfig, NtfyConfig
from mediacore.notify import Notifier, NtfyMessage
from mediacore.paths import to_local, to_remote


def test_config_env_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEKRET", "abc123")
    cfg = parse_config(
        CoreConfig,
        """
        arr:
          radarr:
            base_url: http://radarr:7878
            api_key: ${SEKRET}
        webhook_secret: ${MISSING:-fallback}
        """,
    )
    assert cfg.arr.radarr is not None
    assert cfg.arr.radarr.api_key == "abc123"
    assert cfg.webhook_secret == "fallback"


def test_policy_defaults() -> None:
    cfg = CoreConfig()
    assert cfg.policy.audio_allow == {"ac3", "eac3", "truehd", "mlp", "dts"}
    assert cfg.policy.subtitle_keep_langs == {"eng", "en", "ara", "ar"}


def test_path_map_both_or_neither() -> None:
    with pytest.raises(ValueError):
        ArrInstance(base_url="u", api_key="k", media_root="/data")


def test_path_translation_roundtrip() -> None:
    inst = ArrInstance(
        base_url="u", api_key="k", media_root="/data", local_media_root="/media"
    )
    assert to_local(inst, "/data/movies/X/f.mkv") == Path("/media/movies/X/f.mkv")
    assert to_remote(inst, "/media/movies/X/f.mkv") == Path("/data/movies/X/f.mkv")
    # outside the root passes through
    assert to_local(inst, "/other/f.mkv") == Path("/other/f.mkv")


def test_path_translation_identity_when_unset() -> None:
    inst = ArrInstance(base_url="u", api_key="k")
    assert to_local(inst, "/data/f.mkv") == Path("/data/f.mkv")


def test_parse_sonarr_event() -> None:
    ev = parse_event(
        "sonarr",
        {
            "eventType": "Download",
            "series": {"id": 7, "title": "Show"},
            "episodes": [
                {"seasonNumber": 2, "episodeNumber": 3},
                {"seasonNumber": 2, "episodeNumber": 4},
            ],
            "episodeFile": {"path": "/data/tv/Show/f.mkv"},
            "isUpgrade": True,
        },
    )
    assert ev.app == "sonarr" and ev.item_id == 7
    assert ev.season == 2 and ev.episodes == [3, 4]
    assert ev.label == "Show - S02E03E04"
    assert ev.is_actionable and not ev.is_test


def test_parse_radarr_event() -> None:
    ev = parse_event(
        "radarr",
        {
            "eventType": "Download",
            "movie": {"id": 3, "title": "Mov", "year": 2021},
            "movieFile": {"path": "/data/movies/Mov/f.mkv"},
        },
    )
    assert ev.item_id == 3 and ev.label == "Mov (2021)"
    assert ev.primary_path() == "/data/movies/Mov/f.mkv"


def test_test_event_is_not_actionable() -> None:
    ev = parse_event("radarr", {"eventType": "Test", "movie": {"id": 1, "title": "T"}})
    assert ev.is_test and not ev.is_actionable


def test_audit_record_and_dedup(tmp_path: Path) -> None:
    db = open_db(tmp_path)
    au = Audit(db, tmp_path)
    au.record(
        ActionRecord(
            tool="prune",
            action="prune",
            result=OK,
            path="/x/f.mkv",
            mtime=123.0,
            before={"subs": 3},
            after={"subs": 2},
        )
    )
    assert au.last_done_at("/x/f.mkv", "prune", 123.0) is not None
    assert au.last_done_at("/x/f.mkv", "prune", 999.0) is None  # replaced since: a different file
    assert au.last_done_at("/x/f.mkv", "prune", None) is None
    assert len(au.recent(limit=10)) == 1
    assert len(au.recent(limit=10, results=[OK])) == 1
    assert (tmp_path / "audit").glob("actions-*.jsonl")


def test_audit_stores_duration_ms(tmp_path: Path) -> None:
    au = Audit(open_db(tmp_path), tmp_path)
    au.record(ActionRecord(tool="prune", action="prune", result=OK, duration_ms=90_123))
    au.record(ActionRecord(tool="quality-check", action="check", result=OK))  # no duration
    rows = au.recent(limit=10)
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["prune"]["duration_ms"] == 90_123
    assert by_tool["quality-check"]["duration_ms"] is None


def test_audit_migrates_legacy_table_without_duration(tmp_path: Path) -> None:
    # simulate a db created before the duration_ms column existed
    db = open_db(tmp_path)
    db.executescript(
        "CREATE TABLE actions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,"
        " job_id TEXT, app TEXT, event_type TEXT, tool TEXT NOT NULL, action TEXT NOT NULL,"
        " path TEXT, mtime REAL, result TEXT NOT NULL, before TEXT, after TEXT,"
        " undo_ref TEXT, error TEXT);"
    )
    db.commit()
    au = Audit(db, tmp_path)  # __init__ runs the migration
    au.record(ActionRecord(tool="prune", action="prune", result=OK, duration_ms=42))
    assert au.recent(limit=1)[0]["duration_ms"] == 42


class _FakeResp:
    def raise_for_status(self) -> None:
        pass


def test_notifier_unicode_title_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_post(url, data=None, headers=None, auth=None, timeout=None):
        # requests enforces latin-1 on header values; emulate that to catch regressions.
        for v in (headers or {}).values():
            v.encode("latin-1")
        captured["headers"] = headers
        captured["data"] = data
        return _FakeResp()

    monkeypatch.setattr(notify_mod.requests, "post", fake_post)
    n = Notifier(NtfyConfig(user="u", password="p"))
    # em dash + CJK + emoji in the title, full UTF-8 in the body
    ok = n.send(NtfyMessage(title="Re: ZERO — 進撃 🎬 S04E14", body="body — 全部", tags=["sonarr"]))
    assert ok is True
    assert captured["data"] == "body — 全部".encode()  # body stays UTF-8


def test_notifier_never_raises_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(notify_mod.requests, "post", boom)
    n = Notifier(NtfyConfig(user="u", password="p"))
    assert n.send(NtfyMessage(title="x", body="y", tags=["t"])) is False


def test_shared_connection_survives_concurrent_access(tmp_path: Path) -> None:
    """Regression: the web threadpool + worker share one connection. Concurrent writes and
    aggregate reads must never race into a truncated/None result or raise (see the intermittent
    'NoneType is not subscriptable' 500s on the ledger endpoints)."""
    import threading

    db = open_db(tmp_path)
    db.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER);"
        "INSERT INTO t (n) VALUES (1),(2),(3);"
    )
    db.commit()
    errors: list[Exception] = []

    def writer() -> None:
        try:
            for i in range(200):
                db.execute("UPDATE t SET n=? WHERE id=1", (i,))
                db.commit()
        except Exception as e:
            errors.append(e)

    def reader() -> None:
        try:
            for _ in range(200):
                row = db.execute("SELECT COUNT(*) c, COALESCE(SUM(n),0) s FROM t").fetchone()
                assert row is not None, "aggregate must always return a row"
                assert row["c"] == 3
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent access raised: {errors[:3]}"


def test_the_pre_rename_config_dir_is_still_searched(tmp_path, monkeypatch) -> None:
    """APP_NAME picks an on-disk path, so renaming the project would otherwise make an existing
    ~/.config/media-tools/config.yaml invisible, with no message beyond "no config file found"."""
    from mediacore.config import find_default_config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    legacy = tmp_path / "media-tools"
    legacy.mkdir()
    (legacy / "config.yaml").write_text("policy: {}\n")
    assert find_default_config() == legacy / "config.yaml"

    # ...and the current name wins when both exist
    current = tmp_path / "media-orchestrator"
    current.mkdir()
    (current / "config.yaml").write_text("policy: {}\n")
    assert find_default_config() == current / "config.yaml"


def test_a_missing_config_names_every_directory_it_looked_in(tmp_path, monkeypatch) -> None:
    import pytest
    from mediacore.config import find_default_config

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(FileNotFoundError, match=r"media-orchestrator.*media-tools"):
        find_default_config()


def test_ntfy_headers_transliterate_punctuation_instead_of_replacing_it():
    """Header values must be latin-1, and the em dash the orchestrator's own titles use is not
    in it. Plain `.encode("latin-1","replace")` turned every one into "?" — 88 of 554 messages
    in one measured window read "... .mkv ? failed". Transliterate what we can name, and keep
    the never-raise fallback for what we can't (CJK, emoji)."""
    from mediacore.notify import _header_safe

    assert _header_safe("re-run Bleach - S01E202.mkv — failed") == (
        "re-run Bleach - S01E202.mkv - failed"
    )
    assert _header_safe("a\u2013b\u2026c\u2019d") == "a-b...c'd"
    # anything still unencodable degrades rather than raising
    assert _header_safe("進撃の巨人") == "?????"
