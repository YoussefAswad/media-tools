"""A minimal stdlib progress bar rendered to stderr (no third-party deps)."""

from __future__ import annotations

import sys


class ProgressBar:
    """A single-line ``\\r`` progress bar. A no-op when disabled.

    Enable only for interactive runs: a TTY, not verbose (which prints commands),
    and not dry-run.
    """

    def __init__(self, label: str, *, enabled: bool, width: int = 30) -> None:
        self.label = label
        self.enabled = enabled
        self.width = width
        self._last_pct = -1
        self._finished = False

    def update(self, fraction: float) -> None:
        if not self.enabled or self._finished:
            return
        fraction = 0.0 if fraction < 0 else 1.0 if fraction > 1 else fraction
        pct = int(fraction * 100)
        if pct == self._last_pct:
            return
        self._last_pct = pct
        filled = int(self.width * fraction)
        bar = "#" * filled + "-" * (self.width - filled)
        sys.stderr.write(f"\r  {self.label} [{bar}] {pct:3d}%")
        sys.stderr.flush()

    def finish(self, *, ok: bool = True) -> None:
        if not self.enabled or self._finished:
            return
        self._finished = True
        if ok:
            self.update(1.0)
        sys.stderr.write("\n")
        sys.stderr.flush()


def progress_enabled(*, verbose: bool, dry_run: bool, disabled: bool) -> bool:
    """Progress bars are shown only on an interactive stderr, unless suppressed."""
    if disabled or verbose or dry_run:
        return False
    return sys.stderr.isatty()
