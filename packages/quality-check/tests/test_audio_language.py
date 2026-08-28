"""The audio language filter: keep English/Arabic + the title's original language.

Cases mirror what the real library actually contains (measured before this was built): anime with
a stack of dubs, Nordic and Korean originals, an untagged track, and a title the arr metadata
disagrees with.
"""

from __future__ import annotations

import pytest
from mediacore.models import PolicyConfig
from mediacore.probe import ProbeResult, Track
from quality_check.prune import decide


def mk(mkv_id: int, kind: str, *, language: str = "und", codec_name: str | None = None,
       channels: int | None = None) -> Track:
    return Track(
        mkv_id=mkv_id, ffprobe_index=mkv_id, kind=kind, codec_id="", codec_name=codec_name,
        codec_human=codec_name or "", uid=None, language=language, language_ietf=None,
        track_name="", default=False, forced=False, hearing_impaired=False, visual_impaired=False,
        commentary=False, channels=channels, channel_layout=None, sample_rate=48000,
        bit_rate=None, profile=None, delay_ms=0,
    )


def probe_of(*tracks: Track) -> ProbeResult:
    return ProbeResult(path="/x/f.mkv", tracks=list(tracks), duration_s=1400.0,
                       chapter_entries=0, attachment_count=0)


def policy(**kw) -> PolicyConfig:
    base = {"audio_language_filter": True, "audio_keep_langs": {"eng", "ara"}}
    base.update(kw)
    return PolicyConfig(**base)


def audio(*langs: str) -> ProbeResult:
    return probe_of(mk(0, "video"), *[mk(i, "audio", language=code) for i, code in enumerate(langs, 1)])


# --- the filter is opt-in ------------------------------------------------------------------
def test_filter_is_off_by_default() -> None:
    """Removing audio must never be something an upgrade turns on by itself."""
    d = decide(audio("jpn", "eng", "fre"), PolicyConfig(), original_langs={"jpn"})
    assert d.dropped_audio == [] and d.audio_arg == "all"


# --- the cases from the real library --------------------------------------------------------
def test_anime_keeps_japanese_and_the_english_dub_and_drops_the_rest() -> None:
    pr = audio("jpn", "eng", "spa", "fre", "ger", "ita", "por")
    d = decide(pr, policy(), original_langs={"jpn"})
    assert [t.language for t in d.dropped_audio] == ["spa", "fre", "ger", "ita", "por"]
    assert d.audio_arg == "1,2"
    assert "dropped 5 audio track(s)" in d.summary()


def test_danish_film_keeps_danish_and_would_drop_a_german_dub() -> None:
    d = decide(audio("dan", "ger"), policy(), original_langs={"dan"})
    assert [t.language for t in d.dropped_audio] == ["ger"]
    assert d.keep_audio_ids == [1]


def test_a_single_original_track_is_untouched() -> None:
    """Another Round / The Promised Land: one Danish track, nothing to do."""
    d = decide(audio("dan"), policy(), original_langs={"dan"})
    assert d.dropped_audio == [] and not d.needs_work


def test_korean_original_keeps_the_english_dub_too() -> None:
    d = decide(audio("kor", "kor", "eng"), policy(), original_langs={"kor"})
    assert d.dropped_audio == [] and d.audio_arg == "all"


def test_french_original_matches_either_iso_variant() -> None:
    """Matroska writes 'fre' or 'fra' depending on the muxer; both must count as French."""
    for code in ("fre", "fra"):
        d = decide(audio(code, "eng"), policy(), original_langs={"fre", "fra"})
        assert d.dropped_audio == []


# --- the rails ------------------------------------------------------------------------------
def test_untagged_audio_is_kept() -> None:
    """The Iron Giant: one 'und' track. Unknown is not unwanted."""
    d = decide(audio("und"), policy(), original_langs={"eng"})
    assert d.dropped_audio == [] and d.audio_arg == "all"


def test_untagged_audio_can_be_filtered_when_asked() -> None:
    pr = audio("und", "eng")
    d = decide(pr, policy(audio_keep_untagged=False), original_langs={"eng"})
    assert [t.language for t in d.dropped_audio] == ["und"]


def test_the_filter_is_abandoned_rather_than_leaving_a_file_silent() -> None:
    """A file matching nothing means the policy is wrong about it — never emit '--audio none'."""
    d = decide(audio("ger", "fre"), policy(audio_keep_untagged=False), original_langs={"jpn"})
    assert d.dropped_audio == []
    assert d.audio_filter_abandoned
    assert d.audio_arg == "all"
    assert "language filter would leave none" in d.summary()


def test_an_unknown_original_language_keeps_everything() -> None:
    """No arr record, an unreachable arr, or a placeholder like 'Unknown'.

    Keeping everything must happen *up front* — not by filtering down to eng/ara and then being
    rescued by the never-empty rail, which would still strip the Japanese track off any file that
    also had an English dub.
    """
    d = decide(audio("jpn", "fre"), policy(), original_langs=None)
    assert d.dropped_audio == [] and d.audio_arg == "all"
    assert not d.audio_filter_abandoned, "the rail fired, so filtering ran when it should not have"


def test_unknown_original_does_not_strip_the_original_from_a_dubbed_file() -> None:
    """The regression this guards: jpn + eng with an unresolved original must keep both."""
    d = decide(audio("jpn", "eng"), policy(), original_langs=None)
    assert d.dropped_audio == [] and d.audio_arg == "all"


def test_original_is_ignored_when_keep_original_is_off() -> None:
    d = decide(audio("jpn", "eng"), policy(audio_keep_original=False), original_langs={"jpn"})
    assert [t.language for t in d.dropped_audio] == ["jpn"]


# --- interaction with the rest of the decision ----------------------------------------------
def test_a_dropped_track_is_never_re_encoded() -> None:
    """Encoding a track that is about to be dropped would burn hours for nothing."""
    pr = probe_of(
        mk(0, "video"),
        mk(1, "audio", language="jpn", codec_name="flac", channels=6),  # kept -> re-encode
        mk(2, "audio", language="fre", codec_name="flac", channels=6),  # dropped -> skip
    )
    d = decide(pr, policy(), original_langs={"jpn"})
    assert [t.language for t in d.dropped_audio] == ["fre"]
    assert [t.mkv_id for t in d.audio_encode_tracks] == [1]


def test_audio_arg_never_emits_none() -> None:
    for langs, orig in ((("ger",), {"jpn"}), (("und",), {"jpn"}), ((), {"jpn"})):
        d = decide(audio(*langs), policy(audio_keep_untagged=False), original_langs=orig)
        assert d.audio_arg != "none"


@pytest.mark.parametrize("codes", [{"jpn"}, {"JPN"}])
def test_language_matching_is_case_insensitive(codes) -> None:
    d = decide(audio("jpn", "fre"), policy(), original_langs=codes)
    assert [t.language for t in d.dropped_audio] == ["fre"]
