"""mkvmerge command-shape assertions (no disk writes beyond probing the fixture)."""

from __future__ import annotations

from conftest import requires_tools
from mkvprune.classify import classify_audio
from mkvprune.mux import build_mux_command, build_mux_plan
from mkvprune.probe import probe


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
