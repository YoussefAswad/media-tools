"""Command-line entry point and top-level orchestration."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Callable
from types import FrameType

from mkvprune import __version__
from mkvprune.batch import (
    GROUP_BY_CHOICES,
    FileGroup,
    discover_inputs,
    group_files,
    name_only_collisions,
)
from mkvprune.classify import AudioAction, classify_audio_plan, parse_bitrate
from mkvprune.deps import check_dependencies
from mkvprune.display import render_table
from mkvprune.encode import run_encode
from mkvprune.errors import (
    EXIT_ABORT,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_VERIFY_FAILED,
    MkvPruneError,
    SelectionAbort,
    SelectionSkip,
    VerificationError,
)
from mkvprune.mux import MuxPlan, build_mux_command, build_mux_plan, run_mux
from mkvprune.probe import ProbeResult, probe
from mkvprune.proc import format_command, kill_all_children
from mkvprune.progress import progress_enabled
from mkvprune.replace import (
    check_free_space,
    cleanup,
    estimate_needed_bytes,
    estimate_placement_bytes,
    finalize_output,
    output_dest_path,
    same_filesystem,
    temp_mux_path,
)
from mkvprune.select import SelectionError, confirm, parse_selection, prompt_selection
from mkvprune.subs import SUBTITLE_EXTS, AddedSub, make_added_sub

# Accepted container inputs; output is always Matroska.
ACCEPTED_INPUT_EXTS = {".mkv", ".mp4", ".m4v", ".mov"}

# Intermediate files to remove on signal/failure (never the source).
_INTERMEDIATES: list[str] = []
_KEEP_TEMP = False


def _install_signal_handlers() -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        sys.stderr.write(f"\nmkvprune: received signal {signum}, cleaning up...\n")
        kill_all_children()
        if not _KEEP_TEMP:
            cleanup(_INTERMEDIATES)
        # The source file is never touched here.
        sys.exit(EXIT_ABORT)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mkvprune",
        description="Interactively prune audio/subtitle tracks from an MKV and normalize "
        "audio to Dolby Digital, without touching video/DV/HDR metadata. Given a directory, "
        "files are grouped by track layout and each group is selected once.",
    )
    p.add_argument(
        "input",
        metavar="INPUT",
        help="source file, or a directory to batch (grouped by track layout)",
    )
    p.add_argument(
        "--scratch-dir",
        metavar="DIR",
        help="directory for the temp mux and audio encodes (default: the input's directory)",
    )
    p.add_argument("--audio", metavar="IDS", help="non-interactive audio selection")
    p.add_argument("--subs", metavar="IDS", help="non-interactive subtitle selection")
    p.add_argument(
        "--add-sub",
        metavar="FILE",
        action="append",
        dest="add_sub",
        help="add an external subtitle file (language derived from the filename); the first "
        "added sub becomes the default subtitle. Repeatable.",
    )
    p.add_argument("-y", "--yes", action="store_true", help="skip the final confirmation")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the exact ffmpeg/mkvmerge commands and change nothing",
    )
    p.add_argument(
        "--keep-original",
        action="store_true",
        help="write alongside the source (INPUT.pruned.mkv) instead of replacing it",
    )
    p.add_argument(
        "--keep-temp", action="store_true", help="do not clean up intermediates"
    )
    p.add_argument(
        "--deep-verify",
        action="store_true",
        help="full decode pass during verification (slow)",
    )
    p.add_argument("--ac3-bitrate", metavar="RATE", help="flat AC-3 bitrate (default: ladder)")
    p.add_argument(
        "--eac3-bitrate",
        metavar="RATE",
        help="E-AC-3 bitrate (reserved; >6ch takes the AC-3 compat path)",
    )
    p.add_argument(
        "--group-by",
        choices=GROUP_BY_CHOICES,
        default="name",
        help="batch: how strictly two files count as the same layout. 'name' (default) "
        "compares track names too (whitespace/case normalized); 'structure' ignores names",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="batch: only scan the top level of the directory",
    )
    p.add_argument("--no-progress", action="store_true", help="disable progress bars")
    p.add_argument(
        "--progress-json",
        action="store_true",
        help="emit machine-readable progress as '@@PROGRESS@@ {json}' lines on stdout "
        "(for a calling process; independent of the terminal bar / TTY)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _build_added_subs(paths: list[str] | None) -> list[AddedSub]:
    subs: list[AddedSub] = []
    for i, raw in enumerate(paths or []):
        path = os.path.abspath(raw)
        if not os.path.isfile(path):
            raise MkvPruneError(f"--add-sub file not found: {raw}")
        if os.path.splitext(path)[1].lower() not in SUBTITLE_EXTS:
            print(
                f"  ! --add-sub {os.path.basename(path)}: unrecognized subtitle extension; "
                "letting mkvmerge detect the format",
                file=sys.stderr,
            )
        sub = make_added_sub(path, make_default=(i == 0))
        if sub.language == "und":
            print(
                f"  ! --add-sub {os.path.basename(path)}: could not derive a language from the "
                "filename; tagging as 'und'",
                file=sys.stderr,
            )
        if i > 0:
            print(
                f"  ! --add-sub {os.path.basename(path)}: only the first added subtitle is set "
                "default",
                file=sys.stderr,
            )
        subs.append(sub)
    return subs


def _classify_all(
    pr: ProbeResult, ac3_override: int | None, kept_audio: list[int] | None = None
) -> dict[int, AudioAction]:
    """Classify the file's audio, scoped to ``kept_audio`` when it is known.

    Whether a >6-channel track still needs a downmix depends on what *survives* selection, so
    the plan built in :func:`_plan_for_file` passes the kept ids. The pre-selection pass that
    only renders the track table has no selection yet, and ``None`` (assume everything is kept)
    is the honest preview of the file as it stands.
    """
    return classify_audio_plan(
        pr.audio, kept_ids=kept_audio, ac3_bitrate_override=ac3_override
    )


def _select_ids(
    flag_value: str | None,
    label: str,
    valid_ids: list[int],
    *,
    require_nonempty: bool,
    interactive: bool,
    allow_skip: bool = False,
) -> list[int]:
    if flag_value is not None:
        try:
            chosen = parse_selection(flag_value, valid_ids)
        except SelectionError as exc:
            raise MkvPruneError(f"invalid --{label} selection: {exc}") from exc
        if require_nonempty and not chosen and flag_value.strip().lower() != "none":
            raise MkvPruneError(f"at least one {label} track is required")
        return chosen
    if not interactive:
        # non-interactive but this type had no flag: keep all
        return list(valid_ids)
    return prompt_selection(
        label, valid_ids, require_nonempty=require_nonempty, allow_skip=allow_skip
    )


def _print_plan_summary(plan: MuxPlan) -> None:
    print("\nPlan:")
    for ot in plan.output_tracks:
        marker = "*" if ot.default else " "
        if ot.file_index == 0:
            src = ot.source
            assert src is not None
            print(f"  {marker} {ot.kind:8} keep id {src.mkv_id} ({src.codec_human})")
        elif ot.added_sub is not None:
            sub = ot.added_sub
            flags = ["default"] if ot.default else []
            if sub.forced:
                flags.append("forced")
            if sub.hearing_impaired:
                flags.append("hearing-impaired")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            print(
                f"  {marker} {ot.kind:8} add {os.path.basename(sub.path)} "
                f"(lang={sub.language}){flag_str}"
            )
        else:
            src = ot.source
            assert src is not None
            kind = "compat downmix" if ot.is_added_compat else "converted"
            print(f"  {marker} {ot.kind:8} {kind} from id {src.mkv_id} -> AC-3")
    print("  (* = default track)\n")


def _warn_audio(audio_actions: dict[int, AudioAction], kept_audio: list[int]) -> None:
    """Print each distinct warning once, for the audio tracks actually being kept."""
    warned: set[str] = set()
    for tid in kept_audio:
        for w in audio_actions.get(tid, AudioAction(kind="passthrough")).warnings:
            if w not in warned:
                print(f"  ! {w}", file=sys.stderr)
                warned.add(w)


def _scratch_dir_for(args: argparse.Namespace, input_path: str) -> str:
    if args.scratch_dir:
        return os.path.abspath(args.scratch_dir)
    return os.path.dirname(input_path)


def _plan_for_file(
    pr: ProbeResult,
    args: argparse.Namespace,
    *,
    kept_audio: list[int],
    kept_subtitles: list[int],
    ac3_override: int | None,
    added_subs: list[AddedSub],
) -> tuple[MuxPlan, dict[int, AudioAction]]:
    """Build one file's mux plan.

    Audio is (re)classified per file rather than reused from a batch group: a shared layout
    signature fixes codec and channel count, but not sample rate, so the resample decision
    is genuinely per file.
    """
    input_path = pr.path
    scratch_dir = _scratch_dir_for(args, input_path)
    stem = os.path.splitext(os.path.basename(input_path))[0]
    audio_actions = _classify_all(pr, ac3_override, kept_audio)
    plan = build_mux_plan(
        pr,
        kept_audio_ids=set(kept_audio),
        kept_subtitle_ids=set(kept_subtitles),
        audio_actions=audio_actions,
        input_path=input_path,
        output_path=temp_mux_path(scratch_dir, stem),
        stem=stem,
        scratch_dir=scratch_dir,
        added_subs=added_subs,
    )
    return plan, audio_actions


def _print_dry_run(plan: MuxPlan, args: argparse.Namespace, input_path: str) -> None:
    print("# ffmpeg audio encodes:")
    for job in plan.encode_jobs:
        print(format_command(job.command))
    print("\n# mkvmerge mux:")
    print(format_command(build_mux_command(plan)))
    dest = output_dest_path(input_path, args.keep_original)
    if os.path.abspath(dest) == os.path.abspath(input_path):
        print(f"\n# then atomically replace: {input_path}")
    elif args.keep_original:
        print(f"\n# then write: {dest}")
    else:
        print(f"\n# then write: {dest}\n# and remove the source: {input_path}")


def _progress_emitter():
    """Return a function that writes '@@PROGRESS@@ {json}' progress events to stdout.

    Deduplicates identical (phase, percent) events so a parent process gets one line per
    percent tick per phase.
    """
    import json as _json

    last: dict = {}

    def emit(event: dict) -> None:
        key = (event.get("phase"), event.get("index"))
        if last.get(key) == event.get("percent"):
            return
        last[key] = event.get("percent")
        sys.stdout.write("@@PROGRESS@@ " + _json.dumps(event) + "\n")
        sys.stdout.flush()

    return emit


def _execute_plan(pr: ProbeResult, plan: MuxPlan, args: argparse.Namespace) -> str:
    """Encode, mux, verify, and place one file. Returns the destination path."""
    input_path = plan.input_path
    scratch_dir = os.path.dirname(plan.output_path)

    # Prepare scratch + preflight checks.
    os.makedirs(scratch_dir, exist_ok=True)
    src_size = os.path.getsize(input_path)
    check_free_space(scratch_dir, estimate_needed_bytes(src_size))

    # A scratch dir on another filesystem doesn't make the destination's cost go away, it
    # moves it: finalize has to copy the output back next to the original instead of renaming
    # it. Checking only the scratch volume would green-light a prune that runs for an hour and
    # then fails at the last step with a full media disk. Same-filesystem runs skip this —
    # there the temp mux already occupies the space the check would ask for twice.
    dest_dir = os.path.dirname(os.path.abspath(output_dest_path(input_path, args.keep_original)))
    if not same_filesystem(scratch_dir, dest_dir or "."):
        check_free_space(dest_dir or ".", estimate_placement_bytes(src_size), what="destination")

    # The final output is always written into the source directory.
    if not os.access(os.path.dirname(input_path), os.W_OK):
        raise MkvPruneError(
            f"source directory is not writable (needed to write the output): "
            f"{os.path.dirname(input_path)}"
        )

    # Only ever track the file currently in flight, so the signal handler and the error
    # paths never touch a batch's already-finished work.
    _INTERMEDIATES.clear()
    _INTERMEDIATES.append(plan.output_path)
    for job in plan.encode_jobs:
        _INTERMEDIATES.append(job.output_path)

    show_progress = progress_enabled(
        verbose=args.verbose, dry_run=args.dry_run, disabled=args.no_progress
    )

    # Machine-readable progress for a parent process (independent of the terminal bar / TTY).
    emit = _progress_emitter() if getattr(args, "progress_json", False) else None

    # Encode.
    n_enc = len(plan.encode_jobs)
    for i, job in enumerate(plan.encode_jobs, 1):
        enc_cb = (
            (lambda frac, i=i: emit({"phase": "encode", "index": i, "of": n_enc,
                                     "percent": round(frac * 100)}))
            if emit
            else None
        )
        run_encode(
            job, duration_s=pr.duration_s, progress=show_progress,
            verbose=args.verbose, on_progress=enc_cb,
        )

    # Mux.
    mux_cb = (lambda frac: emit({"phase": "mux", "percent": round(frac * 100)})) if emit else None
    run_mux(plan, progress=show_progress, verbose=args.verbose, on_progress=mux_cb)

    # Verify (raises on failure; leaves temp + original intact).
    verify_cb = (
        (lambda frac: emit({"phase": "verify", "percent": round(frac * 100)})) if emit else None
    )
    return verify_and_finalize(pr, plan, args, src_size, input_path, on_progress=verify_cb)


def run(args: argparse.Namespace) -> int:
    global _KEEP_TEMP
    _KEEP_TEMP = args.keep_temp

    check_dependencies()

    input_path = os.path.abspath(args.input)
    if os.path.isdir(input_path):
        return _run_batch(args, input_path)
    if not os.path.isfile(input_path):
        raise MkvPruneError(f"not a file or directory: {input_path}")
    if os.path.splitext(input_path)[1].lower() not in ACCEPTED_INPUT_EXTS:
        raise MkvPruneError(
            f"unsupported input {input_path!r}; expected one of "
            f"{', '.join(sorted(ACCEPTED_INPUT_EXTS))} (output is always .mkv)"
        )
    return _run_single(args, input_path)


def _run_single(args: argparse.Namespace, input_path: str) -> int:
    ac3_override = parse_bitrate(args.ac3_bitrate) if args.ac3_bitrate else None

    pr = probe(input_path, verbose=args.verbose)
    audio_actions = _classify_all(pr, ac3_override)

    print(f"\n{input_path}\n")
    print(render_table(pr, audio_actions))

    non_interactive = args.audio is not None and args.subs is not None
    interactive = not non_interactive

    audio_valid = [t.mkv_id for t in pr.audio]
    sub_valid = [t.mkv_id for t in pr.subtitles]

    print()
    kept_audio = _select_ids(
        args.audio, "audio", audio_valid, require_nonempty=True, interactive=interactive
    )
    kept_subs = _select_ids(
        args.subs, "subtitle", sub_valid, require_nonempty=False, interactive=interactive
    )

    # Warnings for the tracks we are actually keeping.
    _warn_audio(audio_actions, kept_audio)

    added_subs = _build_added_subs(args.add_sub)

    plan, _ = _plan_for_file(
        pr,
        args,
        kept_audio=kept_audio,
        kept_subtitles=kept_subs,
        ac3_override=ac3_override,
        added_subs=added_subs,
    )
    _print_plan_summary(plan)

    if args.dry_run:
        _print_dry_run(plan, args, input_path)
        return EXIT_OK

    # Confirmation (skipped when fully non-interactive or -y).
    if not args.yes and not non_interactive and not confirm("Proceed?", assume_yes=False):
        print("Aborted.")
        return EXIT_ABORT

    _execute_plan(pr, plan, args)
    return EXIT_OK


# --- batch ---------------------------------------------------------------------------


def _rel(path: str, root: str) -> str:
    return os.path.relpath(path, root)


def _layout_line(group: FileGroup) -> str:
    counts = {"video": 0, "audio": 0, "subtitle": 0}
    for t in group.representative.tracks:
        counts[t.kind] = counts.get(t.kind, 0) + 1
    parts = [f"{n} {kind}" for kind, n in counts.items() if n]
    return ", ".join(parts)


def _print_group_overview(groups: list[FileGroup], root: str) -> None:
    print(f"\n{len(groups)} track layout(s) found:\n")
    for i, group in enumerate(groups, 1):
        print(f"  Group {i}: {len(group)} file(s) — {_layout_line(group)}")
        for pr in group.probes:
            print(f"      {_rel(pr.path, root)}")

    pairs = name_only_collisions(groups)
    if pairs:
        listed = "; ".join(f"{a} and {b}" for a, b in pairs)
        print(
            f"\n  note: group(s) {listed} differ only in track names. "
            "--group-by structure would merge them into one prompt."
        )


def _print_batch_plan(jobs: list[tuple[ProbeResult, MuxPlan]], root: str) -> None:
    print(f"\nBatch plan — {len(jobs)} file(s):")
    for i, (pr, plan) in enumerate(jobs, 1):
        counts = plan.counts()
        kept = ", ".join(f"{n} {kind}" for kind, n in counts.items() if n)
        encodes = len(plan.encode_jobs)
        suffix = f", {encodes} encode(s)" if encodes else ", no encodes"
        print(f"  [{i}/{len(jobs)}] {_rel(pr.path, root)}  ->  {kept}{suffix}")


def _print_batch_summary(
    done: list[str],
    jobs: list[tuple[ProbeResult, MuxPlan]],
    groups: list[FileGroup],
    root: str,
    *,
    failed_at: str | None = None,
) -> None:
    skipped = sum(len(g) for g in groups if g.skipped)
    print("\nBatch summary:")
    print(f"  completed: {len(done)}/{len(jobs)}")
    if skipped:
        skipped_groups = ", ".join(str(i) for i, g in enumerate(groups, 1) if g.skipped)
        print(f"  skipped:   {skipped} file(s) (group {skipped_groups})")
    if failed_at is not None:
        remaining = [_rel(pr.path, root) for pr, _ in jobs[len(done) + 1 :]]
        print(f"  FAILED on: {failed_at}")
        print(f"  not processed: {len(remaining)} file(s) — originals untouched")
        for name in remaining:
            print(f"      {name}")


def _run_batch(args: argparse.Namespace, root: str) -> int:
    if args.add_sub:
        raise MkvPruneError(
            "--add-sub cannot be used with a directory: one subtitle file cannot be the "
            "right one for every input"
        )

    ac3_override = parse_bitrate(args.ac3_bitrate) if args.ac3_bitrate else None
    paths = discover_inputs(
        root, exts=ACCEPTED_INPUT_EXTS, recursive=not args.no_recursive
    )

    print(f"\n{root}")
    print(f"  {len(paths)} input file(s); probing ...")
    probes = [probe(path, verbose=args.verbose) for path in paths]
    groups = group_files(probes, by=args.group_by)

    _print_group_overview(groups, root)

    if (args.audio is not None or args.subs is not None) and len(groups) > 1:
        raise MkvPruneError(
            f"--audio/--subs need a single track layout, but {len(groups)} were found. "
            "A track ID means something different in each, so replaying one selection "
            "across them would keep the wrong tracks. Run interactively, narrow the "
            "directory, or try --group-by structure."
        )

    non_interactive = args.audio is not None and args.subs is not None
    interactive = not non_interactive

    for i, group in enumerate(groups, 1):
        pr = group.representative
        group.audio_actions = _classify_all(pr, ac3_override)
        print(f"\nGroup {i} of {len(groups)} — {len(group)} file(s), "
              f"shown from {_rel(pr.path, root)}\n")
        print(render_table(pr, group.audio_actions))
        print()
        try:
            group.kept_audio = _select_ids(
                args.audio,
                "audio",
                [t.mkv_id for t in pr.audio],
                require_nonempty=True,
                interactive=interactive,
                allow_skip=True,
            )
            group.kept_subtitles = _select_ids(
                args.subs,
                "subtitle",
                [t.mkv_id for t in pr.subtitles],
                require_nonempty=False,
                interactive=interactive,
                allow_skip=True,
            )
        except SelectionSkip:
            group.skipped = True
            print(f"  group {i} skipped; its {len(group)} file(s) stay untouched")
            continue
        _warn_audio(group.audio_actions, group.kept_audio)

    jobs: list[tuple[ProbeResult, MuxPlan]] = []
    for group in groups:
        if group.skipped:
            continue
        for pr in group.probes:
            plan, _ = _plan_for_file(
                pr,
                args,
                kept_audio=group.kept_audio,
                kept_subtitles=group.kept_subtitles,
                ac3_override=ac3_override,
                added_subs=[],
            )
            jobs.append((pr, plan))

    if not jobs:
        print("\nNothing to do.")
        return EXIT_OK

    _print_batch_plan(jobs, root)

    if args.dry_run:
        for pr, plan in jobs:
            print(f"\n### {_rel(pr.path, root)}")
            _print_dry_run(plan, args, pr.path)
        return EXIT_OK

    if (
        not args.yes
        and not non_interactive
        and not confirm(f"\nProcess {len(jobs)} file(s)?", assume_yes=False)
    ):
        print("Aborted.")
        return EXIT_ABORT

    done: list[str] = []
    try:
        for i, (pr, plan) in enumerate(jobs, 1):
            print(f"\n[{i}/{len(jobs)}] {_rel(pr.path, root)}")
            done.append(_execute_plan(pr, plan, args))
    except MkvPruneError:
        # Stop on the first failure: a failure here usually means a wrong assumption that
        # would repeat on every remaining file. Finished files stay finished.
        _print_batch_summary(
            done, jobs, groups, root, failed_at=_rel(jobs[len(done)][0].path, root)
        )
        raise

    _print_batch_summary(done, jobs, groups, root)
    return EXIT_OK


def verify_and_finalize(
    pr: ProbeResult,
    plan: MuxPlan,
    args: argparse.Namespace,
    src_size: int,
    input_path: str,
    *,
    on_progress: Callable[[float], None] | None = None,
) -> str:
    from mkvprune.verify import verify_output

    verify_output(
        pr, plan, src_size=src_size, deep=args.deep_verify, verbose=args.verbose,
        on_progress=on_progress,
    )
    print("  verification passed.")

    dest = finalize_output(
        plan.output_path, input_path, keep_original=args.keep_original
    )
    # The temp mux is now consumed (moved); drop it from the cleanup list.
    if plan.output_path in _INTERMEDIATES:
        _INTERMEDIATES.remove(plan.output_path)

    if not args.keep_temp:
        cleanup([j.output_path for j in plan.encode_jobs])

    print(f"\nDone: {dest}")
    return dest


def main(argv: list[str] | None = None) -> int:
    _install_signal_handlers()
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except SelectionAbort as exc:
        print(f"mkvprune: {exc}", file=sys.stderr)
        if not _KEEP_TEMP:
            cleanup(_INTERMEDIATES)
        return EXIT_ABORT
    except VerificationError as exc:
        # Leave the temp file in place for inspection; the original is untouched.
        print(f"\nmkvprune: verification failed.\n{exc}", file=sys.stderr)
        print(
            f"The temp file was left in place: {_INTERMEDIATES}",
            file=sys.stderr,
        )
        return EXIT_VERIFY_FAILED
    except MkvPruneError as exc:
        print(f"mkvprune: error: {exc}", file=sys.stderr)
        if not _KEEP_TEMP:
            cleanup(_INTERMEDIATES)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
