from __future__ import annotations

from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track
from quality_check.prune import build_mkvprune_argv, decide
from quality_check.rules import check, foreign_image_subs, non_allowed_multichannel


def mk(
    mkv_id: int,
    kind: str,
    *,
    codec_id: str = "",
    codec_name: str | None = None,
    language: str = "und",
    language_ietf: str | None = None,
    track_name: str = "",
    default: bool = False,
    channels: int | None = None,
) -> Track:
    return Track(
        mkv_id=mkv_id,
        ffprobe_index=mkv_id,
        kind=kind,
        codec_id=codec_id,
        codec_name=codec_name,
        codec_human=codec_name or codec_id,
        uid=None,
        language=language,
        language_ietf=language_ietf,
        track_name=track_name,
        default=default,
        forced=False,
        hearing_impaired=False,
        visual_impaired=False,
        commentary=False,
        channels=channels,
        channel_layout=None,
        sample_rate=48000,
        bit_rate=None,
        profile=None,
        delay_ms=0,
    )


def probe_of(*tracks: Track) -> ProbeResult:
    return ProbeResult(
        path="/x/f.mkv",
        tracks=list(tracks),
        duration_s=100.0,
        chapter_entries=0,
        attachment_count=0,
    )


POLICY = PolicyConfig()


def test_foreign_image_subs_selection() -> None:
    pr = probe_of(
        mk(0, "video", codec_id="V_MPEGH/ISO/HEVC", codec_name="hevc"),
        mk(1, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),  # foreign image -> drop
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),  # eng image -> keep
        mk(3, "subtitle", codec_name="subrip", language="fre"),  # text -> keep
        mk(4, "subtitle", codec_name="hdmv_pgs_subtitle", language="und"),  # untagged -> keep (default)
        mk(5, "subtitle", codec_name="hdmv_pgs_subtitle", language="ara"),  # arabic -> keep
    )
    dropped = {t.mkv_id for t in foreign_image_subs(pr, POLICY)}
    assert dropped == {1}


def test_foreign_image_subs_flag_untagged() -> None:
    pr = probe_of(mk(1, "subtitle", codec_name="hdmv_pgs_subtitle", language="und"))
    policy = PolicyConfig(subtitle_flag_untagged=True)
    assert {t.mkv_id for t in foreign_image_subs(pr, policy)} == {1}


def test_non_allowed_multichannel() -> None:
    pr = probe_of(
        mk(1, "audio", codec_name="opus", channels=6),  # flagged
        mk(2, "audio", codec_name="ac3", channels=6),  # allowed
        mk(3, "audio", codec_name="aac", channels=2),  # stereo, under min
    )
    assert {t.mkv_id for t in non_allowed_multichannel(pr, POLICY)} == {1}


def test_check_all_rules() -> None:
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="opus", channels=6),
        mk(2, "audio", codec_name="dts", channels=6),
        mk(3, "audio", codec_name="ac3", channels=6),  # -> 3 audio tracks (> 2) triggers C
        mk(4, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),
    )
    rules = {f.rule for f in check(pr, POLICY)}
    assert rules == {"A", "B", "C"}


def test_decide_prune() -> None:
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="opus", channels=6),  # needs re-encode
        mk(2, "audio", codec_name="dts", channels=8),  # passthrough, kept
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),  # drop
        mk(4, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),  # keep
        mk(5, "subtitle", codec_name="subrip", language="spa"),  # keep (text)
    )
    d = decide(pr, POLICY)
    assert d.needs_work
    assert d.keep_sub_ids == [4, 5]
    assert d.audio_arg == "all"
    assert d.subs_arg == "4,5"
    assert {t.mkv_id for t in d.audio_encode_tracks} == {1}
    argv = build_mkvprune_argv("/x/f.mkv", d)
    assert argv == ["mkvprune", "-y", "--audio", "all", "--subs", "4,5", "/x/f.mkv"]


def test_decide_skip_when_clean() -> None:
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="dts", channels=6),  # passthrough
        mk(2, "subtitle", codec_name="subrip", language="fre"),  # text kept
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),  # eng image kept
    )
    d = decide(pr, POLICY)
    assert not d.needs_work
    assert d.subs_arg == "all"


def test_decide_keep_default_subtitle_guard() -> None:
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre", default=True),
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="ita"),
    )
    guard = PolicyConfig(keep_default_subtitle=True)
    d = decide(pr, guard)
    # the foreign default (id 1) is kept; only id 2 is dropped
    assert d.keep_sub_ids == [1]
    assert {t.mkv_id for t in d.dropped_subs} == {2}
