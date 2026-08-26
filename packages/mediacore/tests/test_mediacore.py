from __future__ import annotations

from pathlib import Path

import pytest
from mediacore.arr.events import parse_event
from mediacore.audit import OK, ActionRecord, Audit, open_db
from mediacore.config import parse_config
from mediacore.models import ArrInstance, CoreConfig
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
    assert "hdmv_pgs_subtitle" in cfg.policy.subtitle_image_codecs
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
    assert ev.label == "Show — S02E03E04"
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
    assert au.already_done("/x/f.mkv", "prune", 123.0) is True
    assert au.already_done("/x/f.mkv", "prune", 999.0) is False
    assert au.already_done("/x/f.mkv", "prune", None) is False
    assert len(au.recent(limit=10)) == 1
    assert len(au.recent(limit=10, results=[OK])) == 1
    assert (tmp_path / "audit").glob("actions-*.jsonl")
