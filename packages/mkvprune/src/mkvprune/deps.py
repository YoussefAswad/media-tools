"""Startup dependency check for the external binaries mkvprune drives."""

from __future__ import annotations

import shutil

from mkvprune.errors import DependencyError

# binary -> package that typically provides it (for the actionable error message)
REQUIRED_BINARIES: dict[str, str] = {
    "mkvmerge": "mkvtoolnix",
    "mkvpropedit": "mkvtoolnix",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffmpeg",
}


def check_dependencies() -> None:
    """Verify every required binary is on PATH.

    Raises DependencyError naming the first missing binary and its package.
    """
    missing: list[str] = []
    for binary, package in REQUIRED_BINARIES.items():
        if shutil.which(binary) is None:
            missing.append(f"  - {binary!r} (install the {package!r} package)")

    if missing:
        raise DependencyError(
            "Missing required program(s) on PATH:\n"
            + "\n".join(missing)
            + "\n\nmkvprune needs mkvtoolnix (mkvmerge, mkvpropedit) and ffmpeg "
            "(ffmpeg, ffprobe)."
        )
