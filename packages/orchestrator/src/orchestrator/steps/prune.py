"""Run mkvprune over the file, and settle what that changed.

The only step that rewrites a media file, and the only one whose failure fails the job.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from datetime import UTC, datetime

from mediacore.arr.events import ArrEvent
from mediacore.arr.languages import iso_for_arr_language
from mediacore.audit import FAIL, OK, SKIP
from mediacore.policy.prune import build_mkvprune_argv, decide

from orchestrator.steps.base import (
    DispatchError,
    InsufficientSpaceError,
    JobContext,
    StepResult,
)
from orchestrator.titles import override_for

log = logging.getLogger(__name__)

#: substring mkvprune emits when its own scratch-space precheck (replace.check_free_space) fails.
_LOW_SPACE_MARKER = "insufficient free space"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _same_filesystem(dir_a: str, dir_b: str) -> bool:
    """Whether two directories live on one volume, so the space guard checks it once.

    Reimplemented here rather than imported from ``mkvprune.replace``: mkvprune is a
    subprocess, never an import (see CLAUDE.md). Unknown (a missing dir) answers False, which
    only costs an extra ``disk_usage`` call that then logs and skips.
    """
    try:
        return os.stat(dir_a).st_dev == os.stat(dir_b).st_dev
    except OSError:
        return False


class PruneStep:
    name = "prune"
    silent = False

    def applies(self, ctx: JobContext) -> bool:
        return True  # being in pipeline.steps is the switch

    def run(self, ctx: JobContext) -> StepResult:
        cfg = ctx.services.config
        local_path = ctx.local_path
        try:
            pr = ctx.probed()
        except Exception as exc:
            return StepResult("mkvprune", "prune", FAIL, error=f"probe: {exc}")

        decision = decide(
            pr, cfg.policy, original_langs=self._original_langs(ctx, local_path)
        )
        if not decision.needs_work:
            return self._settled(ctx, local_path, "mkvprune", SKIP, "nothing to prune/re-encode")

        mtime = os.path.getmtime(local_path) if os.path.exists(local_path) else None
        if ctx.services.audit.already_done(local_path, "prune", mtime):
            return self._settled(
                ctx, local_path, "mkvprune", SKIP, "already pruned (same mtime)"
            )

        # Self-heal: a prune killed mid-mux (e.g. a container restart) leaves a stale
        # {stem}.tmp.mkv that mkvprune refuses to overwrite. Safe to remove — the worker is
        # single-threaded, so nothing else is writing it. The temp lands in the scratch dir,
        # so this has to look where mkvprune would actually write it, not next to the source.
        scratch_dir = cfg.pipeline.scratch_dir
        stem = os.path.splitext(os.path.basename(local_path))[0]
        stale = os.path.join(scratch_dir or os.path.dirname(local_path), stem + ".tmp.mkv")
        if os.path.exists(stale):
            with contextlib.suppress(OSError):
                os.remove(stale)
                log.info("removed stale temp before prune: %s", stale)

        self._ensure_free_space(ctx, local_path)

        argv = build_mkvprune_argv(
            local_path,
            decision,
            executable=cfg.pipeline.mkvprune_executable,
            scratch_dir=scratch_dir,
        )
        argv.insert(1, "--progress-json")  # after the executable
        on_progress = None
        if ctx.set_progress is not None:
            on_progress = lambda pct, note: ctx.set_progress(pct, note)  # noqa: E731
        result = ctx.prune_runner(argv, on_progress=on_progress)
        if not result.ok:
            tail = (result.stderr or result.stdout or "").strip()[-400:]
            # mkvprune runs its own scratch-space precheck; classify that shortfall as deferrable
            # (don't dead-letter for space) even though it surfaced as a subprocess exit.
            if _LOW_SPACE_MARKER in tail.lower():
                raise InsufficientSpaceError(f"mkvprune: {tail}")
            raise DispatchError(f"mkvprune exit {result.returncode}: {tail}")

        self._rescan(ctx.services, ctx.event)
        return self._settled(ctx, local_path, "mkvprune", OK, decision.summary())

    # --- finishing up ---------------------------------------------------------------
    def _settled(
        self, ctx: JobContext, before: str, tool: str, result: str, detail: str
    ) -> StepResult:
        """Build the result, following a cross-format remux and settling the library row.

        A remux (mp4->mkv) writes a sibling .mkv and removes the source, so everything after
        this step — and the job's final path — must follow it. Self-verifying: the rename is
        only believed when the .mkv exists and the source is gone.
        """
        after = os.path.splitext(before)[0] + ".mkv"
        moved = after != before and os.path.exists(after) and not os.path.exists(before)
        final = after if moved else before
        self._sync_library_row(ctx, before, final, result, detail)
        return StepResult(
            "mkvprune", "prune", result, detail, new_path=final if moved else None
        )

    def _sync_library_row(
        self, ctx: JobContext, before: str, after: str, result: str, detail: str
    ) -> None:
        """Bring this file's Library-tab row in line with what just happened to it.

        A file queued from the Library tab would otherwise keep showing its pre-prune decision
        (and a stale "queued" badge) until the next scan. A remux writes a new filename, so that
        row is dropped instead and the next scan indexes the replacement.
        """
        index = ctx.services.library
        if result == FAIL or index.get(before) is None:
            return
        if after != before:
            index.forget(before)
            return
        try:
            st = os.stat(after)
        except OSError:  # gone from under us — the next scan will drop the row anyway
            index.forget(before)
            return
        index.mark_clean(
            after,
            mtime=st.st_mtime,
            size_bytes=st.st_size,
            summary=detail or "nothing to do",
            pruned_at=_now_iso() if result == OK else None,
        )

    # --- policy inputs --------------------------------------------------------------
    def _original_langs(self, ctx: JobContext, local_path: str) -> set[str] | None:
        """ISO codes for this title's original language, for the audio language filter.

        The webhook payload carries ``originalLanguage`` on both apps, so an import needs no
        lookup; a re-run (Library tab, manual ``/rerun``) synthesizes an event without it and
        falls back to the path index. ``None`` = unknown, which keeps every audio track.
        """
        policy = ctx.services.config.policy
        if not policy.audio_language_filter:
            return None
        override = override_for(policy, ctx.event.title)
        if override is not None:
            return override
        isos = iso_for_arr_language(ctx.event.original_language)
        if isos:
            return isos
        try:
            return ctx.services.titles.original_isos(local_path, ctx.event.title or None)
        except Exception as exc:  # the filter degrades to "keep everything", never to a failure
            log.warning("original-language lookup failed for %s: %s", local_path, exc)
            return None

    # --- guards ---------------------------------------------------------------------
    def _ensure_free_space(self, ctx: JobContext, local_path: str) -> None:
        """Refuse to start a prune that can't fit its temp mux. mkvprune writes a
        ``{stem}.tmp.mkv`` (~source size) into its scratch dir before the atomic replace, so peak
        occupancy is ~2x; the *additional* free space required is ~1x the source. Raising here
        defers the job instead of spawning a subprocess that would fail — and instead of
        dead-lettering on a condition that clears itself once disk frees up.

        Both volumes are checked, because a scratch dir on another filesystem needs room on
        *both*: the mux lands in the scratch dir, and finalize then copies the result back
        beside the original (it cannot rename across filesystems). Checking only one would let
        a prune start that is guaranteed to fail on the other. When the scratch dir is on the
        same filesystem as the source it is the same volume and gets checked once.
        """
        try:
            src_size = os.path.getsize(local_path)
        except OSError as exc:
            log.warning("free-space check skipped for %s: %s", local_path, exc)
            return

        cfg = ctx.services.config
        needed = int(src_size * cfg.pipeline.min_free_multiplier)
        source_dir = os.path.dirname(local_path) or "."
        scratch_dir = cfg.pipeline.scratch_dir
        targets = [("source", source_dir)]
        if scratch_dir and not _same_filesystem(scratch_dir, source_dir):
            targets.append(("scratch", scratch_dir))

        for role, path in targets:
            try:
                free = shutil.disk_usage(path).free
            except OSError as exc:
                # An unreachable scratch dir is a misconfiguration, not a space shortfall —
                # deferring would hide it forever. Let mkvprune fail loudly on it instead.
                log.warning("free-space check skipped for %s dir %s: %s", role, path, exc)
                continue
            if free < needed:
                from orchestrator.util import human_bytes

                raise InsufficientSpaceError(
                    f"need ~{human_bytes(needed)} free in the {role} dir ({path}) to prune, "
                    f"have {human_bytes(free)}"
                )

    def _rescan(self, services, event: ArrEvent) -> None:
        if event.item_id is None:
            return
        client = services.arr_client(event.app)
        if client is None:
            return
        try:
            client.rescan_and_wait(event.item_id)
        except Exception as exc:  # non-fatal
            log.warning("%s rescan failed: %s", event.app, exc)
