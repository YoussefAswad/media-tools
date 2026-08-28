"""`subtitle_drop_scope`: drop every foreign sub, or only the bitmap ones.

The motivation is a TV player that fails on many image subs but handles SRT fine — so the point
of `image` scope is to stop flagging files whose only foreign subs are text.
"""

from __future__ import annotations

from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track
from quality_check.rules import foreign_subs, is_image_sub


def sub(mkv_id: int, language: str, codec_name: str, codec_id: str = "") -> Track:
    return Track(
        mkv_id=mkv_id, ffprobe_index=mkv_id, kind="subtitle", codec_id=codec_id,
        codec_name=codec_name, codec_human=codec_name, uid=None, language=language,
        language_ietf=None, track_name="", default=False, forced=False, hearing_impaired=False,
        visual_impaired=False, commentary=False, channels=None, channel_layout=None,
        sample_rate=None, bit_rate=None, profile=None, delay_ms=0,
    )


def probe_of(*tracks: Track) -> ProbeResult:
    return ProbeResult(path="/x/f.mkv", tracks=list(tracks), duration_s=100.0,
                       chapter_entries=0, attachment_count=0)


LIBRARY = probe_of(
    sub(1, "eng", "subrip"),
    sub(2, "fre", "subrip"),                  # foreign text
    sub(3, "ger", "ass"),                     # foreign text
    sub(4, "spa", "hdmv_pgs_subtitle"),       # foreign image
    sub(5, "ita", "dvd_subtitle"),            # foreign image (VobSub)
)


def test_all_scope_drops_text_and_image() -> None:
    got = foreign_subs(LIBRARY, PolicyConfig(subtitle_drop_scope="all"))
    assert [t.mkv_id for t in got] == [2, 3, 4, 5]


def test_image_scope_leaves_foreign_text_alone() -> None:
    got = foreign_subs(LIBRARY, PolicyConfig(subtitle_drop_scope="image"))
    assert [t.mkv_id for t in got] == [4, 5]


def test_image_scope_never_drops_a_kept_language() -> None:
    pr = probe_of(sub(1, "eng", "hdmv_pgs_subtitle"), sub(2, "ara", "dvd_subtitle"))
    assert foreign_subs(pr, PolicyConfig(subtitle_drop_scope="image")) == []


def test_all_is_the_default() -> None:
    assert PolicyConfig().subtitle_drop_scope == "all"


def test_image_detection_falls_back_to_the_mkvmerge_codec_id() -> None:
    """Some tracks report no ffprobe codec name (an mp4 'Timed Text' track, for one); classifying
    by omission would silently treat a bitmap sub as text."""
    policy = PolicyConfig(subtitle_drop_scope="image")
    assert is_image_sub(sub(1, "fre", "", "S_HDMV/PGS"), policy)
    assert is_image_sub(sub(2, "fre", "", "S_VOBSUB"), policy)
    assert not is_image_sub(sub(3, "fre", "", "S_TEXT/UTF8"), policy)
    assert not is_image_sub(sub(4, "fre", "", ""), policy)  # 'Timed Text' -> text


def test_the_image_codec_list_is_configurable() -> None:
    policy = PolicyConfig(subtitle_drop_scope="image", subtitle_image_codecs={"weird_bitmap"})
    pr = probe_of(sub(1, "fre", "weird_bitmap"), sub(2, "fre", "subrip"))
    assert [t.mkv_id for t in foreign_subs(pr, policy)] == [1]


def test_untagged_still_only_counts_when_asked() -> None:
    pr = probe_of(sub(1, "und", "hdmv_pgs_subtitle"))
    assert foreign_subs(pr, PolicyConfig(subtitle_drop_scope="image")) == []
    flagged = PolicyConfig(subtitle_drop_scope="image", subtitle_flag_untagged=True)
    assert [t.mkv_id for t in foreign_subs(pr, flagged)] == [1]
