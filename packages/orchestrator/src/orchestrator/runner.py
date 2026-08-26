"""Subprocess wrapper around the mkvprune CLI (kept behind a small type so it's injectable)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class PruneResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_mkvprune(argv: list[str], *, timeout: float | None = None) -> PruneResult:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return PruneResult(proc.returncode, proc.stdout, proc.stderr)
