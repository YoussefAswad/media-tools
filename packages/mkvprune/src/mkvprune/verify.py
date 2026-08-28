"""Post-mux verification. Every check must pass before the original may be replaced."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from typing import Any, NoReturn

from mkvprune.errors import VerificationError
from mkvprune.mux import MuxPlan
from mkvprune.probe import ProbeResult, probe
from mkvprune.proc import run, run_streamed_split

# video side-data fields that must survive bit-exact
_DOVI_FIELDS = (
    "dv_version_major",
    "dv_version_minor",
    "dv_profile",
    "dv_level",
    "rpu_present_flag",
    "el_present_flag",
    "bl_present_flag",
    "dv_bl_signal_compatibility_id",
)
# mkvmerge video properties that must be identical between source and output
_STABLE_VIDEO_PROP_KEYS = ("codec_id", "uid", "pixel_dimensions", "default_duration")

# Muxer-side chatter from the throwaway `-f null` output, not a defect in the file being
# checked. TrueHD frames are 1/1200s apart while Matroska stores whole-millisecond
# timestamps, so consecutive frames legitimately land on the same DTS — every passthrough
# remux of a TrueHD track produces these, and ffmpeg still exits 0. A copied PGS subtitle
# track produces them too, for the same reason.
#
# "Last message repeated N times" is ffmpeg's own repeat-collapse line: a statement *about*
# the message above it, never an error in its own right. It has to be filtered alongside the
# patterns it collapses — dropping the DTS lines but keeping their companions is what failed
# 30 PGS-bearing files that ffmpeg itself exited 0 on. A genuinely repeated error still
# emits its own first occurrence, which stays fatal.
_BENIGN_FFMPEG_PATTERNS = (
    "non monotonically increasing dts",
    "Non-monotonous DTS in output stream",
    "Last message repeated",
)

# Upper bound on offending lines quoted in a VerificationError; a bad file can emit
# millions.
_MAX_REPORTED_LINES = 20

# Keys emitted by `ffmpeg -progress` (merged into the output stream when we stream for a
# progress readout). These are status lines, not defects, so they're dropped from the error scan.
_PROGRESS_KEYS = frozenset({
    "frame", "fps", "bitrate", "total_size", "out_time_us", "out_time_ms", "out_time",
    "dup_frames", "drop_frames", "speed", "progress",
})


def _fail(check: str, source: Any, output: Any) -> NoReturn:
    raise VerificationError(f"[{check}] mismatch:\n    source: {source}\n    output: {output}")


def _video_stream(pr: ProbeResult) -> dict[str, Any]:
    """The ffprobe stream for the reconciled video track.

    Resolved through the track model rather than by taking the first ``codec_type ==
    "video"`` stream, which would pick up embedded cover art that sorts ahead of the
    real video.
    """
    if not pr.video:
        return {}
    want = pr.video[0].ffprobe_index
    for s in pr.ffprobe_json.get("streams", []):
        if s.get("index") == want:
            return s
    return {}


def _video_props(pr: ProbeResult) -> dict[str, Any]:
    for t in pr.mkvmerge_json.get("tracks", []):
        if t.get("type") == "video":
            return t.get("properties", {})
    return {}


def _side_data_map(stream: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index a stream's side_data_list by side_data_type."""
    out: dict[str, dict[str, Any]] = {}
    for entry in stream.get("side_data_list") or []:
        out[entry.get("side_data_type", "?")] = entry
    return out


def _check_counts(out: ProbeResult, plan: MuxPlan) -> None:
    expected = plan.counts()
    actual = {"video": len(out.video), "audio": len(out.audio), "subtitle": len(out.subtitles)}
    if expected != actual:
        _fail("track composition", expected, actual)


def _check_duration(src: ProbeResult, out: ProbeResult) -> None:
    if src.duration_s is None or out.duration_s is None:
        return
    delta = abs(out.duration_s - src.duration_s)
    if delta > 1.0 and delta > 0.005 * src.duration_s:
        _fail("duration", f"{src.duration_s:.3f}s", f"{out.duration_s:.3f}s")


def _check_video(src: ProbeResult, out: ProbeResult) -> None:
    s, o = _video_stream(src), _video_stream(out)
    for key in ("codec_name", "width", "height", "r_frame_rate"):
        if s.get(key) != o.get(key):
            _fail(f"video {key}", s.get(key), o.get(key))
    # HDR colour identification must survive (only enforce fields the source reports).
    for key in ("color_range", "color_space", "color_transfer", "color_primaries"):
        if s.get(key) is not None and s.get(key) != o.get(key):
            _fail(f"video {key}", s.get(key), o.get(key))
    if s.get("nb_frames") and o.get("nb_frames") and s["nb_frames"] != o["nb_frames"]:
        _fail("video nb_frames", s.get("nb_frames"), o.get("nb_frames"))

    sp, op = _video_props(src), _video_props(out)
    for key in _STABLE_VIDEO_PROP_KEYS:
        # Only enforce when the source actually reports the property. Non-Matroska sources
        # (mp4/mov) have no Matroska track UID, so mkvmerge assigns a fresh one on output.
        if sp.get(key) is not None and sp.get(key) != op.get(key):
            _fail(f"video property {key}", sp.get(key), op.get(key))


def _check_dv_hdr(src: ProbeResult, out: ProbeResult) -> None:
    s_side, o_side = _side_data_map(_video_stream(src)), _side_data_map(_video_stream(out))

    # every side-data type in the source must be present and equal in the output
    for kind, s_entry in s_side.items():
        o_entry = o_side.get(kind)
        if o_entry is None:
            _fail(f"DV/HDR side data '{kind}'", "present", "MISSING")
        if kind == "DOVI configuration record":
            for f in _DOVI_FIELDS:
                if s_entry.get(f) != o_entry.get(f):
                    _fail(f"DOVI {f}", s_entry.get(f), o_entry.get(f))
        else:
            # mastering-display / content-light / others: compare the whole entry
            if s_entry != o_entry:
                _fail(f"HDR side data '{kind}'", s_entry, o_entry)

    # block-addition mapping / DV keys in mkvmerge video properties
    sp, op = _video_props(src), _video_props(out)
    dv_markers = ("dovi", "block_addition", "colour", "color")
    for key in sp:
        low = key.lower()
        if any(m in low for m in dv_markers) and sp.get(key) != op.get(key):
            _fail(f"video property {key}", sp.get(key), op.get(key))


def _check_chapters_attachments(src: ProbeResult, out: ProbeResult) -> None:
    if src.chapter_entries != out.chapter_entries:
        _fail("chapter count", src.chapter_entries, out.chapter_entries)
    if src.attachment_count != out.attachment_count:
        _fail("attachment count", src.attachment_count, out.attachment_count)


def _check_defaults(out: ProbeResult, plan: MuxPlan) -> None:
    # Output track IDs are renumbered, so we compare positionally: the output tracks are in
    # the exact order given to mkvmerge via --track-order, matching plan.output_tracks.
    for kind, tracks in (("audio", out.audio), ("subtitle", out.subtitles)):
        defaults = [t for t in tracks if t.default]
        if tracks and len(defaults) != 1:
            _fail(f"{kind} default count", "exactly 1", len(defaults))
        want_flags = [ot.default for ot in plan.output_tracks if ot.kind == kind]
        got_flags = [t.default for t in tracks]
        if want_flags != got_flags:
            _fail(f"{kind} default placement", want_flags, got_flags)


def _check_size(src_size: int, out_size: int) -> None:
    # The main risk is truncation (output far too small). Some growth is legitimate — a
    # compatibility AC-3 track is additive, and converting near-silent lossless audio to
    # fixed-rate AC-3 can enlarge a small file — so the upper bound is deliberately loose.
    if out_size <= 0:
        _fail("output size", f"{src_size} bytes", f"{out_size} bytes (empty)")
    upper = src_size * 2 + 256 * 1024 * 1024
    if out_size > upper:
        _fail("output size", f"<= {upper} bytes", f"{out_size} bytes")
    if out_size < src_size * 0.05:
        _fail("output size", f">= {int(src_size * 0.05)} bytes (truncation?)", f"{out_size} bytes")


def _fatal_ffmpeg_lines(stderr: str) -> list[str]:
    """Keep only the stderr lines that say something about the *file*.

    ``-f null`` still runs a real muxer, and that muxer complains about its own throwaway
    output. Those complaints are dropped; every other error-level line is treated as a
    genuine problem, because this check gates deleting the user's original.
    """
    return [
        line
        for raw in stderr.splitlines()
        if (line := raw.strip())
        and not _is_progress_line(line)
        and not any(p in line for p in _BENIGN_FFMPEG_PATTERNS)
    ]


def _is_progress_line(line: str) -> bool:
    """True for an ``ffmpeg -progress`` status line (``key=value``), so it isn't mistaken
    for an error when stdout (progress) and stderr are merged."""
    key, sep, _ = line.partition("=")
    if not sep:
        return False
    key = key.strip()
    return key in _PROGRESS_KEYS or key.startswith("stream_")


def _format_ffmpeg_lines(lines: list[str]) -> str:
    """Render offending lines for the error message, capped so it stays readable."""
    head = lines[:_MAX_REPORTED_LINES]
    extra = len(lines) - len(head)
    text = "\n".join(head)
    return f"{text}\n... ({extra} more)" if extra else text


def _check_container_integrity(
    path: str,
    *,
    deep: bool,
    verbose: bool,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> None:
    # -map 0 because ffmpeg's default stream selection reads only the best video and best
    # audio stream and no subtitles at all, leaving most tracks unverified.
    # -progress pipe:1 emits status lines we turn into a fraction; the null output goes to the
    # platform null device (not stdout) so it never collides with the progress stream.
    cmd = ["ffmpeg", "-v", "error", "-nostats", "-progress", "pipe:1", "-i", path, "-map", "0"]
    if deep:
        # Decode video and audio for real, but stream-copy everything else: the null muxer
        # has no encoder for subtitle/attachment/data streams, so re-encoding them would
        # abort the run before a single frame was checked. Copying still demuxes them.
        cmd += ["-c:s", "copy", "-c:t", "copy", "-c:d", "copy"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-f", "null", os.devnull]

    if on_progress is None:
        # No readout wanted: a plain blocking run keeps stdout (progress) and stderr (errors)
        # cleanly separated.
        res = run(cmd, capture=True, verbose=verbose)
    else:
        on_progress(0.0)  # flip the phase to 'verify' immediately, before the first tick

        def on_line(line: str) -> None:
            if duration_s and line.startswith("out_time_us="):
                value = line.split("=", 1)[1].strip()
                with contextlib.suppress(ValueError):
                    on_progress(max(0.0, min(1.0, int(value) / 1_000_000 / duration_s)))
            elif line.strip() == "progress=end":
                on_progress(1.0)

        # Keep stdout (progress) and stderr (errors) on separate pipes — merging them lets the
        # `-progress` ticks interleave with `-v error` muxer chatter and mangle the error lines.
        res = run_streamed_split(cmd, on_stdout_line=on_line, verbose=verbose)

    fatal = _fatal_ffmpeg_lines(res.stderr)
    returncode = res.returncode
    if returncode != 0 or fatal:
        detail = _format_ffmpeg_lines(fatal) or f"exit status {returncode}"
        raise VerificationError(
            f"[container integrity] ffmpeg reported problems in {path}:\n{detail}"
        )


def verify_output(
    src: ProbeResult,
    plan: MuxPlan,
    *,
    src_size: int,
    deep: bool = False,
    verbose: bool = False,
    on_progress: Callable[[float], None] | None = None,
) -> ProbeResult:
    """Run every verification check against the muxed temp file.

    Returns the output ProbeResult on success; raises VerificationError on the first
    failing check (leaving the temp file and original untouched). ``on_progress`` (if given)
    receives the fraction (0..1) of the full-file integrity read — the one slow check — so a
    parent process can show a live 'verifying' readout instead of a stalled 100% mux bar.
    """
    out = probe(plan.output_path, verbose=verbose)  # also proves mkvmerge -J parses

    _check_counts(out, plan)
    _check_duration(src, out)
    _check_video(src, out)
    _check_dv_hdr(src, out)
    _check_chapters_attachments(src, out)
    _check_defaults(out, plan)
    _check_size(src_size, os.path.getsize(plan.output_path))
    _check_container_integrity(
        plan.output_path, deep=deep, verbose=verbose,
        duration_s=src.duration_s, on_progress=on_progress,
    )
    return out
