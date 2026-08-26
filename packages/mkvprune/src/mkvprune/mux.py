"""Build and run the single mkvmerge mux that produces the pruned output.

Chapters, attachments, and global/track tags are copied by mkvmerge by default, so we
never pass ``--no-chapters`` / ``--no-attachments`` / ``--no-global-tags``. Converted audio
tracks are muxed in from standalone intermediate files and placed at their original stream
positions via ``--track-order``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from mkvprune.classify import AudioAction
from mkvprune.encode import EncodeJob, build_encode_job
from mkvprune.errors import MkvPruneError
from mkvprune.flags import resolve_default, source_default_id
from mkvprune.probe import ProbeResult, Track
from mkvprune.proc import format_command, run_streamed
from mkvprune.progress import ProgressBar
from mkvprune.subs import AddedSub

_MKVMERGE_PROGRESS = re.compile(r"Progress:\s*(\d+)%")

COMPAT_SUFFIX = "[5.1 AC-3 compat]"


def compat_track_name(src: Track) -> str:
    base = src.track_name.strip()
    return f"{base} {COMPAT_SUFFIX}" if base else COMPAT_SUFFIX


def best_language(src: Track) -> str:
    """Prefer the IETF/BCP-47 tag, then the legacy code; ``und`` as last resort."""
    for cand in (src.language_ietf, src.language):
        if cand and cand.lower() != "und":
            return cand
    return "und"


@dataclass
class OutputTrack:
    """One track in the final file, in output order."""

    file_index: int  # 0 = source INPUT.mkv, >=1 = an external encoded file
    track_id: int  # track id within that file (0 for a single-track external file)
    kind: str
    source: Track | None  # the source track it represents (None for added external subs)
    is_added_compat: bool = False
    added_sub: AddedSub | None = None  # set for externally added subtitle files
    default: bool = False  # resolved default flag (audio/subtitle only)


@dataclass
class MuxPlan:
    input_path: str
    output_path: str
    kept_audio_source_ids: list[int]
    kept_subtitle_source_ids: list[int]
    encode_jobs: list[EncodeJob]
    output_tracks: list[OutputTrack] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        counts = {"video": 0, "audio": 0, "subtitle": 0}
        for ot in self.output_tracks:
            counts[ot.kind] = counts.get(ot.kind, 0) + 1
        return counts


def build_mux_plan(
    probe: ProbeResult,
    *,
    kept_audio_ids: set[int],
    kept_subtitle_ids: set[int],
    audio_actions: dict[int, AudioAction],
    input_path: str,
    output_path: str,
    stem: str,
    scratch_dir: str,
    added_subs: list[AddedSub] | None = None,
) -> MuxPlan:
    """Assemble the full mux plan: encode jobs, kept source IDs, order, and default flags."""
    encode_jobs: list[EncodeJob] = []
    kept_audio_source_ids: list[int] = []
    output_tracks: list[OutputTrack] = []

    next_file_index = 1

    for t in probe.tracks:
        if t.kind == "video":
            output_tracks.append(OutputTrack(0, t.mkv_id, "video", t))
            continue

        if t.kind == "audio":
            if t.mkv_id not in kept_audio_ids:
                continue
            action = audio_actions[t.mkv_id]
            if action.kind == "passthrough":
                kept_audio_source_ids.append(t.mkv_id)
                output_tracks.append(OutputTrack(0, t.mkv_id, "audio", t))
            elif action.kind == "convert":
                job = build_encode_job(input_path, stem, scratch_dir, t, action)
                job.file_index = next_file_index
                encode_jobs.append(job)
                output_tracks.append(OutputTrack(next_file_index, 0, "audio", t))
                next_file_index += 1
            elif action.kind == "compat":
                # keep the original untouched AND add the downmix
                kept_audio_source_ids.append(t.mkv_id)
                output_tracks.append(OutputTrack(0, t.mkv_id, "audio", t))
                job = build_encode_job(input_path, stem, scratch_dir, t, action)
                job.file_index = next_file_index
                encode_jobs.append(job)
                output_tracks.append(
                    OutputTrack(next_file_index, 0, "audio", t, is_added_compat=True)
                )
                next_file_index += 1
            continue

        # subtitle
        if t.mkv_id in kept_subtitle_ids:
            output_tracks.append(OutputTrack(0, t.mkv_id, "subtitle", t))

    kept_subtitle_source_ids = [
        ot.track_id for ot in output_tracks if ot.kind == "subtitle" and ot.file_index == 0
    ]

    # Append externally added subtitle files after all source-derived tracks.
    for sub in added_subs or []:
        sub.file_index = next_file_index
        output_tracks.append(
            OutputTrack(next_file_index, 0, "subtitle", None, added_sub=sub)
        )
        next_file_index += 1

    _resolve_defaults(probe, output_tracks, "audio")
    _resolve_defaults(probe, output_tracks, "subtitle")

    return MuxPlan(
        input_path=input_path,
        output_path=output_path,
        kept_audio_source_ids=sorted(kept_audio_source_ids),
        kept_subtitle_source_ids=sorted(kept_subtitle_source_ids),
        encode_jobs=encode_jobs,
        output_tracks=output_tracks,
    )


def _resolve_defaults(probe: ProbeResult, output_tracks: list[OutputTrack], kind: str) -> None:
    of_kind = [ot for ot in output_tracks if ot.kind == kind]

    # An explicitly added default subtitle overrides the normal resolution.
    forced_default = next(
        (ot for ot in of_kind if ot.added_sub is not None and ot.added_sub.make_default), None
    )
    if forced_default is not None:
        for ot in of_kind:
            ot.default = ot is forced_default
        return

    candidate_sources = [ot.source for ot in of_kind if not ot.is_added_compat and ot.source]
    chosen_id = resolve_default(candidate_sources, source_default_id(probe.by_kind(kind)))
    for ot in of_kind:
        ot.default = (
            not ot.is_added_compat and ot.source is not None and ot.source.mkv_id == chosen_id
        )


def _added_sub_opts(sub: AddedSub, default: bool) -> list[str]:
    opts = ["--language", f"0:{sub.language}"]
    opts += ["--default-track-flag", f"0:{1 if default else 0}"]
    opts += ["--forced-display-flag", f"0:{1 if sub.forced else 0}"]
    if sub.hearing_impaired:
        opts += ["--hearing-impaired-flag", "0:1"]
    opts.append(sub.path)
    return opts


def build_mux_command(plan: MuxPlan) -> list[str]:
    """Render the mkvmerge command from a plan."""
    cmd: list[str] = ["mkvmerge", "-o", plan.output_path]

    # Source (file 0) track selection.
    if plan.kept_audio_source_ids:
        cmd += ["--audio-tracks", ",".join(str(i) for i in plan.kept_audio_source_ids)]
    else:
        cmd += ["--no-audio"]
    if plan.kept_subtitle_source_ids:
        cmd += ["--subtitle-tracks", ",".join(str(i) for i in plan.kept_subtitle_source_ids)]
    else:
        cmd += ["--no-subtitles"]

    # Default flags for the kept source audio/subtitle tracks.
    for ot in plan.output_tracks:
        if ot.file_index == 0 and ot.kind in {"audio", "subtitle"}:
            cmd += ["--default-track-flag", f"{ot.track_id}:{1 if ot.default else 0}"]

    cmd.append(plan.input_path)

    # External files: encoded audio tracks and added subtitle files.
    job_by_index = {j.file_index: j for j in plan.encode_jobs}
    for ot in plan.output_tracks:
        if ot.file_index == 0:
            continue
        if ot.added_sub is not None:
            cmd += _added_sub_opts(ot.added_sub, ot.default)
            continue
        job = job_by_index[ot.file_index]
        src = ot.source
        assert src is not None
        name = compat_track_name(src) if ot.is_added_compat else src.track_name
        opts = ["--language", f"0:{best_language(src)}"]
        if name:
            opts += ["--track-name", f"0:{name}"]
        opts += ["--default-track-flag", f"0:{1 if ot.default else 0}"]
        forced = 0 if ot.is_added_compat else (1 if src.forced else 0)
        opts += ["--forced-display-flag", f"0:{forced}"]
        if not ot.is_added_compat:
            opts += ["--hearing-impaired-flag", f"0:{1 if src.hearing_impaired else 0}"]
            opts += ["--visual-impaired-flag", f"0:{1 if src.visual_impaired else 0}"]
            opts += ["--commentary-flag", f"0:{1 if src.commentary else 0}"]
        if src.delay_ms:
            opts += ["--sync", f"0:{src.delay_ms}"]
        opts.append(job.output_path)
        cmd += opts

    # Preserve original stream positions.
    order = ",".join(f"{ot.file_index}:{ot.track_id}" for ot in plan.output_tracks)
    cmd += ["--track-order", order]
    return cmd


def run_mux(
    plan: MuxPlan,
    *,
    progress: bool = False,
    verbose: bool = False,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    """Run the mux. Exit 1 (warnings) is non-fatal; exit >=2 is fatal.

    ``on_progress`` (if given) receives the mux fraction (0..1), independent of the terminal
    bar — used to emit machine-readable progress.
    """
    if os.path.exists(plan.output_path):
        raise MkvPruneError(
            f"refusing to overwrite existing temp file: {plan.output_path}\n"
            "Remove it or choose a different --scratch-dir."
        )
    cmd = build_mux_command(plan)
    if not progress:
        print("  muxing with mkvmerge ...")
    bar = ProgressBar("mux", enabled=progress)

    def on_line(line: str) -> None:
        m = _MKVMERGE_PROGRESS.search(line)
        if m:
            frac = int(m.group(1)) / 100
            bar.update(frac)
            if on_progress is not None:
                on_progress(frac)

    res = run_streamed(cmd, on_line=on_line, verbose=verbose)
    bar.finish(ok=res.returncode < 2)

    for line in res.stdout.splitlines():
        line = line.strip()
        if line.lower().startswith("warning"):
            print(f"  mkvmerge {line}")
    if res.returncode >= 2:
        raise MkvPruneError(
            f"mkvmerge failed (exit {res.returncode}):\n  {format_command(cmd)}\n"
            f"{res.stdout.strip()}"
        )
