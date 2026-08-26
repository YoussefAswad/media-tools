"""External subtitle language derivation and added-sub mux behavior."""

from __future__ import annotations

import pytest

from conftest import requires_tools
from mkvprune.classify import classify_audio
from mkvprune.mux import build_mux_command, build_mux_plan
from mkvprune.probe import probe
from mkvprune.subs import derive_subtitle_metadata, make_added_sub


@pytest.mark.parametrize(
    ("name", "lang", "forced", "sdh"),
    [
        ("Movie.en.srt", "en", False, False),
        ("Movie.eng.srt", "eng", False, False),
        ("Movie.spa.forced.srt", "spa", True, False),
        ("Show.S01E01.fr.sdh.ass", "fr", False, True),
        ("Film (2021).German.de.sup", "de", False, False),
        ("subtitle.srt", "und", False, False),
        ("Movie.HDR.2160p.srt", "und", False, False),  # no false-positive language
        ("Movie.forced.srt", "und", True, False),
    ],
)
def test_derive_metadata(name: str, lang: str, forced: bool, sdh: bool) -> None:
    got_lang, got_forced, got_sdh = derive_subtitle_metadata(name)
    assert (got_lang, got_forced, got_sdh) == (lang, forced, sdh)


def test_last_language_token_wins() -> None:
    lang, _, _ = derive_subtitle_metadata("en.dubbed.es.srt")
    assert lang == "es"


@requires_tools
def test_added_sub_is_default_in_mux(sample_mkv, tmp_path) -> None:
    ext = tmp_path / "Movie.spa.forced.srt"
    ext.write_text("1\n00:00:00,000 --> 00:00:01,000\nhola\n")

    pr = probe(str(sample_mkv))
    actions = {t.mkv_id: classify_audio(t) for t in pr.audio}
    plan = build_mux_plan(
        pr,
        kept_audio_ids={1},
        kept_subtitle_ids={5},  # keep the English source sub too
        audio_actions=actions,
        input_path=str(sample_mkv),
        output_path=str(sample_mkv.with_suffix(".tmp.mkv")),
        stem=sample_mkv.stem,
        scratch_dir=str(sample_mkv.parent),
        added_subs=[make_added_sub(str(ext), make_default=True)],
    )
    # subtitle count = 1 source-kept + 1 added
    assert plan.counts()["subtitle"] == 2

    # the added sub is the sole default subtitle; the source sub is cleared
    sub_tracks = [ot for ot in plan.output_tracks if ot.kind == "subtitle"]
    added = [ot for ot in sub_tracks if ot.added_sub is not None]
    source_sub = [ot for ot in sub_tracks if ot.added_sub is None]
    assert added[0].default is True
    assert all(not ot.default for ot in source_sub)

    cmd = build_mux_command(plan)
    # the added file appears with its derived language, forced flag, and default flag
    assert str(ext) in cmd
    i = cmd.index(str(ext))
    window = cmd[max(0, i - 8) : i]
    assert "0:spa" in window
    assert "--forced-display-flag" in window
    # exactly one subtitle default overall
    assert cmd[cmd.index("--track-order") + 1].endswith(f"{added[0].file_index}:0")
