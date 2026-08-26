"""Build and run the per-track ffmpeg audio encodes.

Each convert/compat action produces one standalone intermediate file. Language, track
name, delay, and flags are NOT written here — they are applied at the mkvmerge mux step.
"""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass
from functools import lru_cache

from mkvprune.classify import AudioAction, format_bitrate
from mkvprune.errors import MkvPruneError
from mkvprune.probe import Track
from mkvprune.proc import format_command, run, run_streamed
from mkvprune.progress import ProgressBar

# canonical channel layout name for a given channel count, used when the source layout
# is unknown/unspecified so the encoder gets a defined mapping.
_CANONICAL_LAYOUT = {1: "mono", 2: "stereo", 3: "3.0", 4: "quad", 5: "5.0", 6: "5.1", 8: "7.1"}

_UNKNOWN_LAYOUTS = {"", "unknown", "none"}


@lru_cache(maxsize=1)
def channel_layout_flag() -> str:
    """Return the ffmpeg channel-layout flag this build understands."""
    res = run(["ffmpeg", "-hide_banner", "-h", "full"], capture=True)
    text = f"{res.stdout}{res.stderr}"
    return "-ch_layout" if "-ch_layout " in text or "-ch_layout\n" in text else "-channel_layout"


@dataclass
class EncodeJob:
    source: Track
    action: AudioAction
    output_path: str
    command: list[str]
    file_index: int = 0  # mkvmerge input-file index in the mux (assigned by the planner)

    @property
    def is_added_compat(self) -> bool:
        return self.action.kind == "compat"


def output_filename(scratch_dir: str, stem: str, track_id: int, ext: str) -> str:
    return os.path.join(scratch_dir, f"{stem}.track{track_id}.{ext}")


def build_encode_job(
    input_path: str,
    stem: str,
    scratch_dir: str,
    track: Track,
    action: AudioAction,
) -> EncodeJob:
    """Construct the ffmpeg command for one convert/compat action."""
    if not action.is_encode:
        raise MkvPruneError(f"track {track.mkv_id} is not an encode action")

    out = output_filename(scratch_dir, stem, track.mkv_id, action.file_ext)
    cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        input_path,
        "-map",
        f"0:{track.ffprobe_index}",
        "-vn",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
    ]

    if action.kind == "compat":
        # explicit downmix to 5.1 (6 ch) — never rely on the encoder silently downmixing
        cmd += ["-ac", "6"]
    else:
        layout = (track.channel_layout or "").lower()
        if layout in _UNKNOWN_LAYOUTS and track.channels in _CANONICAL_LAYOUT:
            cmd += [channel_layout_flag(), _CANONICAL_LAYOUT[track.channels]]

    if action.resample_to:
        cmd += ["-ar", str(action.resample_to)]

    assert action.target_codec and action.bitrate_bps
    cmd += ["-c:a", action.target_codec, "-b:a", format_bitrate(action.bitrate_bps), out]

    return EncodeJob(source=track, action=action, output_path=out, command=cmd)


def run_encode(
    job: EncodeJob,
    *,
    duration_s: float | None = None,
    progress: bool = False,
    verbose: bool = False,
) -> None:
    """Execute one encode job, raising MkvPruneError on failure."""
    label = f"encode t{job.source.mkv_id} -> {os.path.basename(job.output_path)}"
    if not progress:
        print(f"  {label}")
    bar = ProgressBar(label, enabled=progress)

    def on_line(line: str) -> None:
        if duration_s and line.startswith("out_time_us="):
            value = line.split("=", 1)[1].strip()
            with contextlib.suppress(ValueError):
                bar.update(int(value) / 1_000_000 / duration_s)
        elif line.strip() == "progress=end":
            bar.update(1.0)

    res = run_streamed(job.command, on_line=on_line, verbose=verbose)
    bar.finish(ok=res.returncode == 0)

    if res.returncode != 0:
        raise MkvPruneError(
            f"ffmpeg failed (exit {res.returncode}) encoding track {job.source.mkv_id}:\n"
            f"  {format_command(job.command)}\n{res.stdout.strip()}"
        )
    if not os.path.exists(job.output_path) or os.path.getsize(job.output_path) == 0:
        raise MkvPruneError(
            f"ffmpeg produced no output for track {job.source.mkv_id}: {job.output_path}"
        )
