"""mkvmerge-id <-> ffprobe-index reconciliation, including fail-loud on mismatch."""

from __future__ import annotations

import pytest

import mkvprune.probe as probe_mod
from conftest import requires_tools
from mkvprune.errors import ProbeError


def _fake_run_json(mkv: dict, ff_full: dict, ff_side: dict):
    """Return a run_json replacement that dispatches by the ffprobe/mkvmerge command."""

    def dispatch(cmd, *, verbose=False):
        if cmd[0] == "mkvmerge":
            return mkv
        if "-show_entries" in cmd:
            return ff_side
        return ff_full

    return dispatch


def test_mapping_excludes_attachment_stream(monkeypatch) -> None:
    # 2 real tracks in mkvmerge, but ffprobe also reports an attachment stream in the middle,
    # which must be excluded so ordinals still line up.
    mkv = {
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {"codec_id": "V_MPEGH/ISO/HEVC", "uid": 11}},
            {"id": 1, "type": "audio", "codec": "AC-3", "properties": {"codec_id": "A_AC3", "uid": 22}},
        ],
        "chapters": [],
        "attachments": [{"id": 1}],
    }
    ff_full = {
        "format": {"duration": "1.000"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "attachment", "codec_name": "ttf"},
            {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6, "channel_layout": "5.1"},
        ],
    }
    monkeypatch.setattr(probe_mod, "run_json", _fake_run_json(mkv, ff_full, {"streams": []}))
    result = probe_mod.probe("x.mkv")

    assert [t.mkv_id for t in result.tracks] == [0, 1]
    audio = result.audio[0]
    # the audio track's ffprobe index is 2 (after the attachment), not its mkvmerge id (1)
    assert audio.ffprobe_index == 2
    assert audio.mkv_id == 1
    assert audio.channels == 6


def test_mapping_excludes_cover_art_stream(monkeypatch) -> None:
    # MP4 cover art: mkvmerge calls it an attachment, ffprobe calls it a video stream
    # flagged attached_pic. It must not be counted as a track.
    mkv = {
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}},
            {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"codec_id": "A_EAC3"}},
        ],
        "chapters": [],
        "attachments": [{"id": 0}],
    }
    ff_full = {
        "format": {"duration": "1.0"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "eac3", "channels": 6},
            {
                "index": 2,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
            },
        ],
    }
    monkeypatch.setattr(probe_mod, "run_json", _fake_run_json(mkv, ff_full, {"streams": []}))
    result = probe_mod.probe("x.mp4")

    assert [t.mkv_id for t in result.tracks] == [0, 1]
    assert [t.ffprobe_index for t in result.tracks] == [0, 1]
    assert "mjpeg" not in [t.codec_name for t in result.tracks]
    assert result.attachment_count == 1


def test_mapping_excludes_leading_cover_art_stream(monkeypatch) -> None:
    # Same thing, but the artwork sorts *before* the real video.
    mkv = {
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}},
            {"id": 1, "type": "audio", "codec": "E-AC-3", "properties": {"codec_id": "A_EAC3"}},
        ],
        "chapters": [],
        "attachments": [{"id": 0}],
    }
    ff_full = {
        "format": {"duration": "1.0"},
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "disposition": {"attached_pic": 1},
            },
            {"index": 1, "codec_type": "video", "codec_name": "hevc"},
            {"index": 2, "codec_type": "audio", "codec_name": "eac3", "channels": 6},
        ],
    }
    monkeypatch.setattr(probe_mod, "run_json", _fake_run_json(mkv, ff_full, {"streams": []}))
    result = probe_mod.probe("x.mp4")

    assert [t.mkv_id for t in result.tracks] == [0, 1]
    # the real video is ffprobe index 1, behind the artwork
    assert result.video[0].ffprobe_index == 1
    assert result.audio[0].ffprobe_index == 2


def test_mapping_fails_loud_on_codec_mismatch(monkeypatch) -> None:
    mkv = {
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}},
            {"id": 1, "type": "audio", "codec": "AC-3", "properties": {"codec_id": "A_AC3"}},
        ],
        "chapters": [],
        "attachments": [],
    }
    ff_full = {
        "format": {"duration": "1.0"},
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {"index": 1, "codec_type": "audio", "codec_name": "dts"},  # A_AC3 vs dts
        ],
    }
    monkeypatch.setattr(probe_mod, "run_json", _fake_run_json(mkv, ff_full, {"streams": []}))
    with pytest.raises(ProbeError, match="Codec mismatch"):
        probe_mod.probe("x.mkv")


def test_mapping_fails_loud_on_count_mismatch(monkeypatch) -> None:
    mkv = {
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": {"codec_id": "V_MPEGH/ISO/HEVC"}},
            {"id": 1, "type": "audio", "codec": "AC-3", "properties": {"codec_id": "A_AC3"}},
        ],
        "chapters": [],
        "attachments": [],
    }
    ff_full = {"format": {}, "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc"}]}
    monkeypatch.setattr(probe_mod, "run_json", _fake_run_json(mkv, ff_full, {"streams": []}))
    with pytest.raises(ProbeError, match="count mismatch") as excinfo:
        probe_mod.probe("x.mkv")

    # the message must name both sides so the failure is diagnosable on its own
    message = str(excinfo.value)
    assert "id 0: video (HEVC)" in message
    assert "id 1: audio (AC-3)" in message
    assert "index 0: video (hevc)" in message


@requires_tools
def test_probe_real_fixture(sample_mkv) -> None:
    result = probe_mod.probe(str(sample_mkv))
    assert len(result.video) == 1
    assert len(result.audio) == 4
    assert len(result.subtitles) == 2
    assert result.chapter_entries == 2
    assert result.attachment_count == 1
    # ffprobe indices are contiguous 0..6 and match the mkvmerge track order
    assert [t.ffprobe_index for t in result.tracks] == [0, 1, 2, 3, 4, 5, 6]
    assert result.audio[0].audio_codec == "aac"
    assert result.audio[2].channels == 8  # the FLAC 7.1


@requires_tools
def test_probe_real_mp4_with_cover_art(sample_mp4_with_cover) -> None:
    result = probe_mod.probe(str(sample_mp4_with_cover))
    assert len(result.video) == 1
    assert len(result.audio) == 1
    assert len(result.subtitles) == 0
    # the artwork is an attachment, not a track
    assert result.attachment_count == 1
    assert result.video[0].codec_name == "h264"
