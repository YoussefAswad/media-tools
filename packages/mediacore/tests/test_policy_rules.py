from __future__ import annotations

from mediacore.models import PolicyConfig
from mediacore.policy.prune import build_mkvprune_argv, decide
from mediacore.policy.rules import check, foreign_subs, non_allowed_multichannel
from mediacore.probe import ProbeResult, Track


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


def probe_of(*tracks: Track, path: str = "/x/f.mkv") -> ProbeResult:
    return ProbeResult(
        path=path,
        tracks=list(tracks),
        duration_s=100.0,
        chapter_entries=0,
        attachment_count=0,
    )


POLICY = PolicyConfig()


def test_foreign_subs_selection() -> None:
    pr = probe_of(
        mk(0, "video", codec_id="V_MPEGH/ISO/HEVC", codec_name="hevc"),
        mk(1, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),  # foreign image -> drop
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),  # eng image -> keep
        mk(3, "subtitle", codec_name="subrip", language="fre"),  # foreign TEXT -> now dropped too
        mk(4, "subtitle", codec_name="hdmv_pgs_subtitle", language="und"),  # untagged -> keep (default)
        mk(5, "subtitle", codec_name="hdmv_pgs_subtitle", language="ara"),  # arabic -> keep
        mk(6, "subtitle", codec_name="subrip", language="eng"),  # eng text -> keep
    )
    dropped = {t.mkv_id for t in foreign_subs(pr, POLICY)}
    assert dropped == {1, 3}  # both image AND text foreign subs


def test_foreign_subs_flag_untagged() -> None:
    pr = probe_of(mk(1, "subtitle", codec_name="subrip", language="und"))
    policy = PolicyConfig(subtitle_flag_untagged=True)
    assert {t.mkv_id for t in foreign_subs(pr, policy)} == {1}


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
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),  # drop (image)
        mk(4, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),  # keep
        mk(5, "subtitle", codec_name="subrip", language="spa"),  # drop (foreign text now)
    )
    d = decide(pr, POLICY)
    assert d.needs_work
    assert d.keep_sub_ids == [4]  # only eng survives (spa text now dropped)
    assert d.audio_arg == "all"
    assert d.subs_arg == "4"
    assert {t.mkv_id for t in d.audio_encode_tracks} == {1}
    argv = build_mkvprune_argv("/x/f.mkv", d)
    assert argv == ["mkvprune", "-y", "--audio", "all", "--subs", "4", "/x/f.mkv"]


def test_decide_skip_when_clean() -> None:
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="dts", channels=6),  # passthrough
        mk(2, "subtitle", codec_name="subrip", language="eng"),  # eng text kept
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="ara"),  # ara image kept
    )
    d = decide(pr, POLICY)
    assert not d.needs_work
    assert d.subs_arg == "all"


def _clean_tracks():
    # a clean file: no foreign subs, no re-encode needed
    return (
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="dts", channels=6),  # passthrough
        mk(2, "subtitle", codec_name="subrip", language="eng"),
    )


REMUX = PolicyConfig(remux_to_mkv=True)


def test_remux_forces_work_on_clean_mp4() -> None:
    d = decide(probe_of(*_clean_tracks(), path="/x/movie.mp4"), REMUX)
    assert d.remux and d.needs_work
    assert d.audio_arg == "all" and d.subs_arg == "all"  # keep everything
    assert "remux" in d.summary()


def test_remux_not_forced_on_clean_mkv() -> None:
    d = decide(probe_of(*_clean_tracks(), path="/x/movie.mkv"), REMUX)
    assert not d.remux and not d.needs_work


def test_remux_ignores_unsupported_container() -> None:
    # .avi is not an accepted mkvprune input — never force a remux that would just fail
    d = decide(probe_of(*_clean_tracks(), path="/x/movie.avi"), REMUX)
    assert not d.remux and not d.needs_work


def test_remux_toggle_off_leaves_clean_mp4_alone() -> None:
    d = decide(probe_of(*_clean_tracks(), path="/x/movie.mp4"), POLICY)  # remux_to_mkv False
    assert not d.remux and not d.needs_work


def test_remux_mp4_also_applies_subs_and_audio_policy() -> None:
    # an mp4 that ALSO trips the existing rules still drops foreign subs + re-encodes
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="opus", channels=6),  # re-encode
        mk(2, "subtitle", codec_name="subrip", language="fre"),  # drop
        mk(3, "subtitle", codec_name="subrip", language="eng"),  # keep
        path="/x/movie.mp4",
    )
    d = decide(pr, REMUX)
    assert d.remux and d.needs_work
    assert d.subs_arg == "3" and {t.mkv_id for t in d.audio_encode_tracks} == {1}


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


def _dirty_decision():
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="opus", channels=6),
        mk(2, "subtitle", codec_name="hdmv_pgs_subtitle", language="fre"),
        mk(3, "subtitle", codec_name="hdmv_pgs_subtitle", language="eng"),
    )
    return decide(pr, POLICY)


def test_argv_omits_scratch_dir_by_default() -> None:
    # No staging configured must reproduce the old command exactly.
    assert "--scratch-dir" not in build_mkvprune_argv("/x/f.mkv", _dirty_decision())


def test_argv_carries_scratch_dir() -> None:
    argv = build_mkvprune_argv("/x/f.mkv", _dirty_decision(), scratch_dir="/scratch")
    assert argv[argv.index("--scratch-dir") + 1] == "/scratch"
    assert argv[-1] == "/x/f.mkv"  # the input path stays last (positional)


# --- the AC-3 compatibility downmix ---------------------------------------------------
#
# The >6-channel compat path keeps the source and *adds* a downmix, which makes it the one
# branch of the classifier that is not idempotent. These pin both halves of the fix: a file
# that already has its downmix is finished, and one carrying the damage gets cleaned up.

COMPAT = "[5.1 AC-3 compat]"


def _twin_peaks(*, compat_tracks: int, default_on_compat: bool = False):
    """The real shape this was found on: one 7.1 AAC source, N identical AC-3 downmixes."""
    tracks = [
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="aac", channels=8, language="eng",
           default=not default_on_compat),
    ]
    for i in range(compat_tracks):
        tracks.append(
            mk(2 + i, "audio", codec_name="ac3", channels=6, language="eng",
               track_name=COMPAT, default=default_on_compat and i == 0)
        )
    tracks.append(mk(2 + compat_tracks, "subtitle", codec_name="hdmv_pgs_subtitle",
                     language="eng"))
    return probe_of(*tracks)


def test_decide_is_a_no_op_once_the_downmix_is_there() -> None:
    """The regression that matters: a second run of the pipeline must find nothing to do.
    Before the fix this re-added the downmix every single time."""
    d = decide(_twin_peaks(compat_tracks=1, default_on_compat=True), POLICY)
    assert not d.needs_work
    assert d.audio_encode_tracks == []
    assert d.surplus_compat == []


def test_decide_drops_a_duplicate_downmix() -> None:
    d = decide(_twin_peaks(compat_tracks=2, default_on_compat=True), POLICY)
    assert d.needs_work
    assert [t.mkv_id for t in d.surplus_compat] == [3]  # the first one stays
    assert [t.mkv_id for t in d.dropped_audio] == [3]
    assert d.dropped_by_language == []  # not a language drop, and must not read as one
    assert d.audio_encode_tracks == []  # and emphatically not a third encode
    assert d.audio_arg == "1,2"
    assert "duplicate AC-3 compat" in d.summary()


def test_decide_moves_the_default_onto_the_downmix() -> None:
    """Files pruned before the downmix took the default flag still point players at the 7.1
    track they cannot decode. A plain remux settles it, so there is nothing to drop."""
    d = decide(_twin_peaks(compat_tracks=1), POLICY)
    assert d.needs_work
    assert d.compat_default_fix
    assert d.dropped_audio == []
    assert d.audio_arg == "all"
    assert d.audio_encode_tracks == []


def test_decide_still_adds_a_missing_downmix() -> None:
    d = decide(_twin_peaks(compat_tracks=0), POLICY)
    assert d.needs_work
    assert {t.mkv_id for t in d.audio_encode_tracks} == {1}
    assert not d.compat_default_fix  # nothing to move it onto yet


def test_finding_a_stops_once_the_downmix_is_there() -> None:
    """Rule A warns about audio the player cannot handle. Once the downmix is in the file that
    is no longer true, but the source stays non-Dolby by design — so without this the finding
    fires on every scan, forever."""
    assert non_allowed_multichannel(_twin_peaks(compat_tracks=0), POLICY)
    assert not non_allowed_multichannel(_twin_peaks(compat_tracks=1), POLICY)


def test_a_five_one_aac_is_still_reported() -> None:
    """The suppression is scoped to the compat path: a 5.1 AAC is re-encoded in place, so it is
    still a finding and still work to do."""
    pr = probe_of(
        mk(0, "video", codec_name="hevc"),
        mk(1, "audio", codec_name="aac", channels=6, language="eng", default=True),
        mk(2, "audio", codec_name="ac3", channels=6, language="eng", track_name=COMPAT),
    )
    assert [t.mkv_id for t in non_allowed_multichannel(pr, POLICY)] == [1]
    assert {t.mkv_id for t in decide(pr, POLICY).audio_encode_tracks} == {1}
