"""The --progress-json emitter contract (used by the orchestrator to stream live progress)."""

from __future__ import annotations

import json

from mkvprune.cli import _progress_emitter


def test_progress_emitter_format_and_dedup(capsys):
    emit = _progress_emitter()
    emit({"phase": "encode", "index": 1, "of": 2, "percent": 40})
    emit({"phase": "encode", "index": 1, "of": 2, "percent": 40})  # dup -> suppressed
    emit({"phase": "encode", "index": 1, "of": 2, "percent": 41})
    emit({"phase": "mux", "percent": 41})  # different phase, same percent -> emitted
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 3
    for line in out:
        assert line.startswith("@@PROGRESS@@ ")
        json.loads(line[len("@@PROGRESS@@ "):])  # valid JSON payload
    first = json.loads(out[0][len("@@PROGRESS@@ "):])
    assert first == {"phase": "encode", "index": 1, "of": 2, "percent": 40}
