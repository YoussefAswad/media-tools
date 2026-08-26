"""Batch mode: input discovery, layout grouping, and the guardrails around track IDs."""

from __future__ import annotations

import pytest

from conftest import make_track
from mkvprune import cli
from mkvprune.batch import (
    discover_inputs,
    group_files,
    layout_signature,
    name_only_collisions,
)
from mkvprune.errors import MkvPruneError, SelectionSkip
from mkvprune.probe import ProbeResult
from mkvprune.select import parse_selection

EXTS = {".mkv", ".mp4", ".m4v", ".mov"}


# --- fixtures for synthetic episode layouts -----------------------------------------


def _video(**kw):
    base = {
        "mkv_id": 0, "ffprobe_index": 0, "kind": "video", "codec_id": "V_MPEGH/ISO/HEVC",
        "codec_name": "hevc", "codec_human": "HEVC", "language": "und",
        "language_ietf": None, "channels": None, "channel_layout": None,
        "sample_rate": None, "default": True,
    }
    return make_track(**{**base, **kw})


def _audio(mkv_id, index, channels, name="", **kw):
    base = {
        "mkv_id": mkv_id, "ffprobe_index": index, "kind": "audio", "codec_id": "A_AAC",
        "codec_name": "aac", "codec_human": "AAC", "language": "eng",
        "language_ietf": None, "channels": channels,
        "channel_layout": "5.1" if channels == 6 else "mono",
        "sample_rate": 48000, "track_name": name, "default": channels == 6,
    }
    return make_track(**{**base, **kw})


def _sub(mkv_id, index, name="", **kw):
    base = {
        "mkv_id": mkv_id, "ffprobe_index": index, "kind": "subtitle",
        "codec_id": "S_HDMV/PGS", "codec_name": "hdmv_pgs_subtitle",
        "codec_human": "HDMV PGS", "language": "eng", "language_ietf": None,
        "channels": None, "channel_layout": None, "sample_rate": None,
        "track_name": name, "default": False,
    }
    return make_track(**{**base, **kw})


def _episode(path: str, *, extra_sub: bool = False, commentary_sub_name: str) -> ProbeResult:
    """The real Band of Brothers shape: video + AAC 5.1 + AAC mono commentary + PGS subs.

    ``extra_sub`` reproduces E02, whose additional subtitle shifts the commentary sub from
    id 5 to id 6.
    """
    tracks = [
        _video(),
        _audio(1, 1, 6),
        _audio(2, 2, 1, name="Audio commentary by the Easy Company veterans"),
        _sub(3, 3),
        _sub(4, 4),
    ]
    next_id = 5
    if extra_sub:
        tracks.append(_sub(next_id, next_id))
        next_id += 1
    tracks.append(_sub(next_id, next_id, name=commentary_sub_name))
    return ProbeResult(
        path=path, tracks=tracks, duration_s=3600.0, chapter_entries=11, attachment_count=0
    )


def _e01():
    # lowercase "audio" — the real E01 spelling
    return _episode("/s/E01.mkv", commentary_sub_name="for audio commentary by the veterans")


def _e02():
    return _episode(
        "/s/E02.mkv", extra_sub=True, commentary_sub_name="for Audio commentary by the veterans"
    )


def _e03():
    return _episode("/s/E03.mkv", commentary_sub_name="for Audio commentary by the veterans")


def _e04():
    # missing the "for " prefix — the real E04 spelling
    return _episode("/s/E04.mkv", commentary_sub_name="Audio commentary by the veterans")


# --- discovery ------------------------------------------------------------------------


def test_discover_inputs_recurses_and_sorts(tmp_path) -> None:
    season = tmp_path / "Season 1"
    season.mkdir()
    for name in ("E02.mkv", "E01.mkv", "E03.mp4"):
        (season / name).write_text("x")
    (tmp_path / "poster.jpg").write_text("x")
    (tmp_path / "notes.txt").write_text("x")

    found = discover_inputs(str(tmp_path), exts=EXTS)
    assert [p.rsplit("/", 1)[-1] for p in found] == ["E01.mkv", "E02.mkv", "E03.mp4"]


def test_discover_inputs_skips_our_own_outputs(tmp_path) -> None:
    for name in ("show.mkv", "show.tmp.mkv", "show.pruned.mkv"):
        (tmp_path / name).write_text("x")

    found = discover_inputs(str(tmp_path), exts=EXTS)
    assert [p.rsplit("/", 1)[-1] for p in found] == ["show.mkv"]


def test_discover_inputs_no_recursive_stays_top_level(tmp_path) -> None:
    (tmp_path / "top.mkv").write_text("x")
    nested = tmp_path / "Season 1"
    nested.mkdir()
    (nested / "deep.mkv").write_text("x")

    assert len(discover_inputs(str(tmp_path), exts=EXTS, recursive=False)) == 1
    assert len(discover_inputs(str(tmp_path), exts=EXTS, recursive=True)) == 2


def test_discover_inputs_empty_directory_raises(tmp_path) -> None:
    (tmp_path / "readme.txt").write_text("x")
    with pytest.raises(MkvPruneError, match="no supported input files"):
        discover_inputs(str(tmp_path), exts=EXTS)


# --- signatures and grouping ----------------------------------------------------------


def test_shifted_track_ids_produce_different_signatures() -> None:
    # The core safety property: E02's extra subtitle moves the commentary sub from id 5 to
    # id 6, so the two files must never share a group and thus never share an ID selection.
    assert layout_signature(_e01()) != layout_signature(_e02())
    assert layout_signature(_e01(), by="structure") != layout_signature(_e02(), by="structure")


def test_identical_layouts_share_a_signature() -> None:
    a = _episode("/s/A.mkv", commentary_sub_name="Commentary")
    b = _episode("/s/B.mkv", commentary_sub_name="Commentary")
    assert layout_signature(a) == layout_signature(b)


def test_track_name_case_and_spacing_do_not_split_a_group() -> None:
    a = _episode("/s/A.mkv", commentary_sub_name="for Audio  commentary")
    b = _episode("/s/B.mkv", commentary_sub_name="FOR audio commentary")
    assert layout_signature(a) == layout_signature(b)


def test_group_files_matches_the_real_directory_shape() -> None:
    probes = [_e01(), _e02(), _e03(), _e04()]

    by_name = group_files(probes, by="name")
    assert [len(g) for g in by_name] == [2, 1, 1]  # (E01+E03), E02, E04
    assert by_name[0].paths == ["/s/E01.mkv", "/s/E03.mkv"]

    by_structure = group_files(probes, by="structure")
    assert [len(g) for g in by_structure] == [3, 1]  # (E01+E03+E04), E02


def test_name_only_collisions_flags_the_name_variant_only() -> None:
    groups = group_files([_e01(), _e02(), _e04()], by="name")
    # groups 1 (E01) and 3 (E04) differ only in a track name; E02 differs structurally.
    assert name_only_collisions(groups) == [(1, 3)]


def test_name_only_collisions_empty_when_layouts_really_differ() -> None:
    groups = group_files([_e01(), _e02()], by="name")
    assert name_only_collisions(groups) == []


# --- CLI guardrails -------------------------------------------------------------------


def _args(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def _stub_discovery(monkeypatch, probes: dict[str, ProbeResult]) -> None:
    monkeypatch.setattr(cli, "discover_inputs", lambda *_a, **_k: list(probes))
    monkeypatch.setattr(cli, "probe", lambda path, **_k: probes[path])


def _record_executions(monkeypatch) -> list[str]:
    """Replace the per-file execution step, recording which files it was handed."""
    executed: list[str] = []

    def fake(pr, _plan, _args):
        executed.append(pr.path)
        return pr.path

    monkeypatch.setattr(cli, "_execute_plan", fake)
    return executed


def _answer_prompts(monkeypatch, answers: list[str]) -> None:
    """Drive the real _select_ids through a scripted prompt_selection.

    prompt_selection binds ``input`` as a default argument at definition time, so patching
    builtins.input would not be seen; patching the function cli calls keeps parse_selection
    and the SelectionSkip plumbing real.
    """
    remaining = iter(answers)

    def fake(label, valid_ids, *, require_nonempty, allow_skip=False):
        raw = next(remaining)
        if allow_skip and raw.strip().lower() == "skip":
            raise SelectionSkip(f"skipped at {label} selection")
        return parse_selection(raw, valid_ids)

    monkeypatch.setattr(cli, "prompt_selection", fake)


def test_add_sub_is_rejected_for_a_directory(tmp_path) -> None:
    args = _args([str(tmp_path), "--add-sub", "x.srt"])
    with pytest.raises(MkvPruneError, match="--add-sub cannot be used with a directory"):
        cli._run_batch(args, str(tmp_path))


def test_id_flags_rejected_when_layouts_differ(monkeypatch, tmp_path) -> None:
    _stub_discovery(monkeypatch, {"/s/E01.mkv": _e01(), "/s/E02.mkv": _e02()})

    args = _args([str(tmp_path), "--audio", "1", "--subs", "3,4"])
    with pytest.raises(MkvPruneError, match="need a single track layout"):
        cli._run_batch(args, str(tmp_path))


def test_id_flags_allowed_for_a_single_layout(monkeypatch, tmp_path) -> None:
    _stub_discovery(monkeypatch, {"/s/E01.mkv": _e01(), "/s/E03.mkv": _e03()})
    executed = _record_executions(monkeypatch)

    args = _args([str(tmp_path), "--audio", "1", "--subs", "3,4"])
    assert cli._run_batch(args, str(tmp_path)) == 0
    assert executed == ["/s/E01.mkv", "/s/E03.mkv"]


# --- batch flow -----------------------------------------------------------------------


def test_skipped_group_is_left_untouched(monkeypatch, tmp_path) -> None:
    _stub_discovery(
        monkeypatch, {"/s/E01.mkv": _e01(), "/s/E03.mkv": _e03(), "/s/E02.mkv": _e02()}
    )
    executed = _record_executions(monkeypatch)
    # group 1 (E01+E03): keep all audio, subs 3,4 — group 2 (E02): skip
    _answer_prompts(monkeypatch, ["all", "3,4", "skip"])

    args = _args([str(tmp_path), "-y"])
    assert cli._run_batch(args, str(tmp_path)) == 0
    assert executed == ["/s/E01.mkv", "/s/E03.mkv"]  # E02 never processed


def test_batch_stops_on_first_failure(monkeypatch, tmp_path, capsys) -> None:
    _stub_discovery(monkeypatch, {"/s/E01.mkv": _e01(), "/s/E03.mkv": _e03()})

    attempted: list[str] = []

    def boom(pr, _plan, _args):
        attempted.append(pr.path)
        raise MkvPruneError("mkvmerge exploded")

    monkeypatch.setattr(cli, "_execute_plan", boom)

    args = _args([str(tmp_path), "--audio", "1", "--subs", "3,4"])
    with pytest.raises(MkvPruneError, match="mkvmerge exploded"):
        cli._run_batch(args, str(tmp_path))

    assert attempted == ["/s/E01.mkv"]  # the second file was never started
    out = capsys.readouterr().out
    assert "completed: 0/2" in out
    assert "FAILED on" in out
    assert "originals untouched" in out
