"""mkvmerge command-shape assertions (no disk writes beyond probing the fixture)."""

from __future__ import annotations

from conftest import make_track, requires_tools
from mkvprune.classify import classify_audio
from mkvprune.mux import OutputTrack, _resolve_defaults, build_mux_command, build_mux_plan
from mkvprune.probe import ProbeResult, probe


def _plan_for(sample_mkv, kept_audio, kept_subs):
    pr = probe(str(sample_mkv))
    actions = {t.mkv_id: classify_audio(t) for t in pr.audio}
    plan = build_mux_plan(
        pr,
        kept_audio_ids=set(kept_audio),
        kept_subtitle_ids=set(kept_subs),
        audio_actions=actions,
        input_path=str(sample_mkv),
        output_path=str(sample_mkv.with_suffix(".tmp.mkv")),
        stem=sample_mkv.stem,
        scratch_dir=str(sample_mkv.parent),
    )
    return pr, plan


@requires_tools
def test_plan_composition(sample_mkv) -> None:
    _, plan = _plan_for(sample_mkv, kept_audio=[1, 2, 3, 4], kept_subs=[5])
    # id 2 (FLAC 5.1) is converted -> excluded from the source passthrough list;
    # id 3 (FLAC 7.1) keeps the original -> included.
    assert plan.kept_audio_source_ids == [1, 3, 4]
    assert plan.kept_subtitle_source_ids == [5]
    # two encodes: one convert (id 2), one compat downmix (id 3)
    kinds = sorted(j.action.kind for j in plan.encode_jobs)
    assert kinds == ["compat", "convert"]
    counts = plan.counts()
    assert counts == {"video": 1, "audio": 5, "subtitle": 1}  # 3 kept + 1 convert + 1 compat


@requires_tools
def test_command_shape(sample_mkv) -> None:
    _, plan = _plan_for(sample_mkv, kept_audio=[1, 2, 3, 4], kept_subs=[5])
    cmd = build_mux_command(plan)

    # container-level content is copied by default; never suppressed
    for forbidden in ("--no-chapters", "--no-attachments", "--no-global-tags", "--no-video"):
        assert forbidden not in cmd

    assert "--audio-tracks" in cmd
    assert cmd[cmd.index("--audio-tracks") + 1] == "1,3,4"
    assert cmd[cmd.index("--subtitle-tracks") + 1] == "5"

    order = cmd[cmd.index("--track-order") + 1]
    assert order == "0:0,0:1,1:0,0:3,2:0,0:4,0:5"

    # exactly one default among the source audio tracks and one among subtitles
    pairs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--default-track-flag"]
    assert pairs.count("1:1") == 1  # AAC (source default) stays default
    assert "5:1" in pairs  # the kept English subtitle becomes default

    # the compat downmix track carries a marked name and the source language
    assert any(v.endswith("[5.1 AC-3 compat]") for v in cmd)
    assert "0:en" in cmd  # external tracks carry the IETF language


@requires_tools
def test_all_audio_converted_uses_no_audio(sample_mkv) -> None:
    # Keep only the FLAC 5.1 track (id 2), which is converted -> no source audio survives.
    _, plan = _plan_for(sample_mkv, kept_audio=[2], kept_subs=[])
    cmd = build_mux_command(plan)
    assert "--no-audio" in cmd
    assert "--no-subtitles" in cmd
    assert plan.kept_audio_source_ids == []


# --- the default flag belongs on the compatibility downmix ----------------------------
#
# These drive _resolve_defaults directly: it is pure track bookkeeping, and going through
# build_mux_plan would drag in an ffmpeg probe for the encode job's channel-layout flag.


def _probe_of(*tracks) -> ProbeResult:
    return ProbeResult(
        path="/x/f.mkv", tracks=list(tracks), duration_s=60.0,
        chapter_entries=0, attachment_count=0,
    )


def _aac_71(mkv_id: int, **overrides):
    fields = {
        "mkv_id": mkv_id, "codec_id": "A_AAC", "codec_name": "aac", "codec_human": "AAC",
        "channels": 8,
    }
    return make_track(**{**fields, **overrides})


def _ac3(mkv_id: int, **overrides):
    fields = {
        "mkv_id": mkv_id, "codec_id": "A_AC3", "codec_name": "ac3", "codec_human": "AC-3",
        "channels": 6,
    }
    return make_track(**{**fields, **overrides})


def test_added_downmix_takes_the_default_from_its_source() -> None:
    """The downmix exists because a limited player cannot decode the source. Pointing that
    player at the source anyway defeats the whole exercise."""
    src = _aac_71(1, default=True)
    out = [OutputTrack(0, 1, "audio", src), OutputTrack(1, 0, "audio", src, is_added_compat=True)]
    _resolve_defaults(_probe_of(src), out, "audio")
    assert [ot.default for ot in out] == [False, True]


def test_downmix_does_not_displace_a_track_that_is_already_playable() -> None:
    """It stands in only for the source it was made from. An authored DTS 5.1 that wins the
    default needs no stand-in — it is decodable as it is."""
    dts = make_track(
        mkv_id=1, codec_id="A_DTS", codec_name="dts", codec_human="DTS", channels=6, default=True
    )
    src = _aac_71(2)
    out = [
        OutputTrack(0, 1, "audio", dts),
        OutputTrack(0, 2, "audio", src),
        OutputTrack(1, 0, "audio", src, is_added_compat=True),
    ]
    _resolve_defaults(_probe_of(dts, src), out, "audio")
    assert [ot.default for ot in out] == [True, False, False]


def test_a_downmix_already_in_the_file_takes_the_default() -> None:
    """The migration case, and the reason this is not just a check on is_added_compat: on a
    re-mux there is no compat *action* at all (the classifier suppresses it as redundant), so
    without this the flag would sit on the 7.1 source forever."""
    src = _aac_71(1, default=True)
    old = _ac3(2, track_name="[5.1 AC-3 compat]")
    out = [OutputTrack(0, 1, "audio", src), OutputTrack(0, 2, "audio", old)]
    _resolve_defaults(_probe_of(src, old), out, "audio")
    assert [ot.default for ot in out] == [False, True]


def test_a_correctly_flagged_file_is_left_alone() -> None:
    src = _aac_71(1)
    old = _ac3(2, track_name="[5.1 AC-3 compat]", default=True)
    out = [OutputTrack(0, 1, "audio", src), OutputTrack(0, 2, "audio", old)]
    _resolve_defaults(_probe_of(src, old), out, "audio")
    assert [ot.default for ot in out] == [False, True]


@requires_tools
def test_command_marks_the_downmix_default(sample_mkv) -> None:
    # Keep only the FLAC 7.1 track (id 3): it wins the default, and its downmix stands in.
    _, plan = _plan_for(sample_mkv, kept_audio=[3], kept_subs=[])
    cmd = build_mux_command(plan)
    pairs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--default-track-flag"]
    assert "3:0" in pairs  # the 7.1 source gives the flag up
    assert pairs.count("0:1") == 1  # the external downmix file takes it
