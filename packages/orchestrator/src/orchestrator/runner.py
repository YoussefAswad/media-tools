"""Subprocess wrapper around the mkvprune CLI (kept behind a small type so it's injectable).

Streams mkvprune's stdout so ``--progress-json`` events (``@@PROGRESS@@ {json}`` lines) can be
surfaced live via an ``on_progress(percent, note)`` callback, throttled so the DB isn't hammered.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

PROGRESS_PREFIX = "@@PROGRESS@@ "

# on_progress(percent: float 0-100, note: str)
ProgressCB = Callable[[float, str], None]


@dataclass
class PruneResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _note(event: dict) -> str:
    phase = event.get("phase", "")
    pct = event.get("percent", 0)
    if phase == "encode":
        return f"encoding audio {event.get('index', 1)}/{event.get('of', 1)} · {pct}%"
    if phase == "mux":
        return f"muxing · {pct}%"
    if phase == "verify":
        return f"verifying · {pct}%"
    return f"{phase} · {pct}%"


def run_mkvprune(
    argv: list[str],
    *,
    on_progress: ProgressCB | None = None,
    timeout: float | None = None,
) -> PruneResult:
    """Run mkvprune, streaming stdout. Progress lines drive ``on_progress`` (throttled to ≥1%
    change and ≥1s between updates); all output is still collected for the result."""
    if on_progress is None:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return PruneResult(proc.returncode, proc.stdout, proc.stderr)

    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    lines: list[str] = []
    last_pct = -1.0
    last_t = 0.0
    assert proc.stdout is not None
    for raw in proc.stdout:
        lines.append(raw)
        if raw.startswith(PROGRESS_PREFIX):
            try:
                event = json.loads(raw[len(PROGRESS_PREFIX):])
            except ValueError:
                continue
            pct = float(event.get("percent", 0))
            now = time.monotonic()
            if pct >= 100 or (abs(pct - last_pct) >= 1 and now - last_t >= 1.0):
                last_pct, last_t = pct, now
                # progress reporting must never break the run
                with contextlib.suppress(Exception):
                    on_progress(pct, _note(event))
    rc = proc.wait(timeout=timeout)
    return PruneResult(rc, "".join(lines), "")
