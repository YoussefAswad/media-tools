"""Codec classification, the bitrate ladder, and the >6ch compatibility path."""

from __future__ import annotations

import pytest

from conftest import make_track
from mkvprune.classify import (
    AC3_MAX_BITRATE_BPS,
    ac3_ladder_bitrate,
    classify_audio,
    format_bitrate,
    parse_bitrate,
)


@pytest.mark.parametrize(
    ("codec_id", "codec_name"),
    [
        ("A_AC3", "ac3"),
        ("A_EAC3", "eac3"),
        ("A_TRUEHD", "truehd"),
        ("A_DTS", "dts"),
        ("A_MLP", "mlp"),
    ],
)
def test_passthrough_codecs(codec_id: str, codec_name: str) -> None:
    t = make_track(codec_id=codec_id, codec_name=codec_name, channels=6)
    assert classify_audio(t).kind == "passthrough"


def test_stereo_convertible_kept() -> None:
    # stereo FLAC/AAC/Opus are NOT re-encoded (only multichannel is).
    for codec_id, codec_name in [("A_FLAC", "flac"), ("A_AAC", "aac"), ("A_OPUS", "opus")]:
        t = make_track(codec_id=codec_id, codec_name=codec_name, channels=2, channel_layout="stereo")
        assert classify_audio(t).kind == "passthrough"


def test_mono_convertible_kept() -> None:
    t = make_track(codec_id="A_AAC", codec_name="aac", channels=1, channel_layout="mono")
    assert classify_audio(t).kind == "passthrough"


def test_unknown_channels_kept_with_warning() -> None:
    t = make_track(codec_id="A_FLAC", codec_name="flac", channels=None, channel_layout=None)
    action = classify_audio(t)
    assert action.kind == "passthrough"
    assert action.warnings


def test_multichannel_flac_converts_to_ac3() -> None:
    t = make_track(codec_id="A_FLAC", codec_name="flac", channels=6, channel_layout="5.1")
    action = classify_audio(t)
    assert action.kind == "convert"
    assert action.target_codec == "ac3"
    assert action.bitrate_bps == 640_000
    assert action.target_channels == 6


def test_three_channel_converts() -> None:
    t = make_track(codec_id="A_PCM", codec_name="pcm_s24le", channels=3, channel_layout="3.0")
    assert classify_audio(t).kind == "convert"


def test_seven_one_compat_path() -> None:
    t = make_track(codec_id="A_FLAC", codec_name="flac", channels=8, channel_layout="7.1")
    action = classify_audio(t)
    assert action.kind == "compat"
    assert action.target_codec == "ac3"
    assert action.target_channels == 6
    assert action.bitrate_bps == 640_000
    assert action.warnings  # prominent warning present


def test_high_sample_rate_resamples() -> None:
    t = make_track(codec_id="A_FLAC", codec_name="flac", channels=6, sample_rate=96000)
    action = classify_audio(t)
    assert action.resample_to == 48000
    assert any("resampl" in w for w in action.warnings)


def test_ac3_bitrate_override_flat() -> None:
    t = make_track(codec_id="A_FLAC", codec_name="flac", channels=6)
    action = classify_audio(t, ac3_bitrate_override=parse_bitrate("448k"))
    assert action.bitrate_bps == 448_000


def test_ac3_bitrate_override_capped() -> None:
    t = make_track(codec_id="A_FLAC", codec_name="flac", channels=6)
    action = classify_audio(t, ac3_bitrate_override=parse_bitrate("700k"))
    assert action.bitrate_bps == AC3_MAX_BITRATE_BPS
    assert any("640k" in w for w in action.warnings)


@pytest.mark.parametrize(
    ("channels", "expected"),
    [(1, 224_000), (2, 448_000), (3, 640_000), (6, 640_000)],
)
def test_ladder(channels: int, expected: int) -> None:
    assert ac3_ladder_bitrate(channels) == expected


@pytest.mark.parametrize(
    ("text", "bps"),
    [("640k", 640_000), ("1536k", 1_536_000), ("448000", 448_000), ("1.5m", 1_500_000)],
)
def test_parse_bitrate(text: str, bps: int) -> None:
    assert parse_bitrate(text) == bps


def test_format_bitrate() -> None:
    assert format_bitrate(640_000) == "640k"
    assert format_bitrate(448_123) == "448123"


def test_classify_ignores_track_name() -> None:
    # A track *named* "Dolby Digital 5.1" that is really FLAC must still convert.
    t = make_track(
        codec_id="A_FLAC", codec_name="flac", channels=6, track_name="Dolby Digital 5.1"
    )
    assert classify_audio(t).kind == "convert"
