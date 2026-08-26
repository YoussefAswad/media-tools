from __future__ import annotations

import sys

from orchestrator.runner import run_mkvprune


def test_run_mkvprune_parses_progress_events():
    """A fake 'mkvprune' that prints @@PROGRESS@@ lines drives on_progress and is captured."""
    script = (
        "import sys,time\n"
        "print('@@PROGRESS@@ {\"phase\":\"encode\",\"index\":1,\"of\":1,\"percent\":50}')\n"
        "print('normal output line')\n"
        "print('@@PROGRESS@@ {\"phase\":\"mux\",\"percent\":100}')\n"
        "sys.exit(0)\n"
    )
    seen: list = []
    res = run_mkvprune([sys.executable, "-c", script], on_progress=lambda p, n: seen.append((p, n)))
    assert res.returncode == 0
    # both progress ticks reported (100 always passes the throttle)
    pcts = [p for p, _ in seen]
    assert 50 in pcts and 100 in pcts
    notes = [n for _, n in seen]
    assert any("encoding audio 1/1" in n for n in notes)
    assert any("muxing" in n for n in notes)
    # full output (incl. progress + normal lines) preserved for error tails
    assert "normal output line" in res.stdout


def test_run_mkvprune_without_callback_is_blocking():
    res = run_mkvprune([sys.executable, "-c", "print('hi')"])
    assert res.returncode == 0 and "hi" in res.stdout
