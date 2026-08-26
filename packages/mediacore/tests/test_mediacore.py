from __future__ import annotations

from pathlib import Path

import pytest
from mediacore import notify as notify_mod
from mediacore.arr.events import parse_event
from mediacore.audit import OK, ActionRecord, Audit, open_db
from mediacore.config import parse_config
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
    assert au.already_done("/x/f.mkv", "prune", 123.0) is True
    assert au.already_done("/x/f.mkv", "prune", 999.0) is False
    assert au.already_done("/x/f.mkv", "prune", None) is False
    assert len(au.recent(limit=10)) == 1
    assert len(au.recent(limit=10, results=[OK])) == 1
    assert (tmp_path / "audit").glob("actions-*.jsonl")


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
