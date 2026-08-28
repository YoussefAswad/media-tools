"""Walking the configured roots and refreshing the index.

Read-only by construction: probe + ``decide()``, which is pure. Nothing here writes to a media
file — enqueueing the work it finds is a separate, explicit act from the Library tab.

Expensive, though: each file costs an mkvmerge plus a few ffprobe calls, on the single worker
thread, so a full library blocks imports for as long as it runs. Three things keep that honest —
the (path, mtime, size, scan_version) cache, a ``prune_missing`` scoped to the roots actually
walked, and a pause check every 25 files so "Pause worker" still drains promptly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from mediacore.policy.prune import PruneDecision, decide
from mediacore.probe import ProbeResult
from mediacore.probe import probe as _default_probe

from orchestrator.library.estimate import estimate_delta
from orchestrator.library.index import (
    AUDIO,
    AUDIO_DROP,
    REMUX,
    SCAN_VERSION,
    SUBS,
    LibraryRow,
    _scan_token,
)

log = logging.getLogger(__name__)

@dataclass
class ScanStats:
    """What one scan pass did. ``probed`` is the expensive part; ``cached`` is what the
    (path, mtime) index saved."""

    files: int = 0
    probed: int = 0
    cached: int = 0
    needs_work: int = 0
    errors: int = 0
    removed: int = 0
    stopped: bool = False
    roots: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{self.files} files", f"{self.probed} probed", f"{self.needs_work} need work"]
        if self.cached:
            parts.append(f"{self.cached} unchanged")
        if self.errors:
            parts.append(f"{self.errors} errors")
        if self.removed:
            parts.append(f"{self.removed} gone")
        if self.stopped:
            parts.append("stopped early")
        return " · ".join(parts)


def resolve_roots(cfg) -> list[tuple[Path, str]]:
    """(path, app) pairs to walk. Explicit ``library.roots`` win; otherwise each configured arr
    instance contributes its library mount as this container sees it. Nothing at all when the
    feature is switched off, which is what makes ``library.enabled`` a real off switch."""
    lib = cfg.library
    if not lib.enabled:
        return []
    if lib.roots:
        return [(r.path, r.app) for r in lib.roots]
    out: list[tuple[Path, str]] = []
    for app in ("radarr", "sonarr"):
        inst = getattr(cfg.arr, app, None)
        if inst is None:
            continue
        root = inst.local_media_root or inst.media_root
        if root is not None:
            out.append((Path(root), app))
    return out


def iter_files(root: Path, exts: set[str], min_size: int) -> Iterator[tuple[str, os.stat_result]]:
    """Media files under ``root``, skipping hidden dirs, mkvprune's in-flight temp files and
    anything below ``min_size`` (samples, featurettes, stray artwork)."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith(".") or fn.endswith(".tmp.mkv"):
                continue
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            full = os.path.join(dirpath, fn)
            try:
                st = os.stat(full)
            except OSError:  # vanished mid-walk / unreadable — nothing to index
                continue
            if st.st_size < min_size:
                continue
            yield full, st


def _row_for(
    path: str,
    st: os.stat_result,
    app: str,
    root: str,
    pr: ProbeResult,
    decision: PruneDecision,
    pruned_at: str | None,
    scanned_at: str,
    original_lang: str | None = None,
) -> LibraryRow:
    reasons: list[str] = []
    if decision.dropped_subs:
        reasons.append(SUBS)
    if decision.audio_encode_tracks:
        reasons.append(AUDIO)
    if decision.remux:
        reasons.append(REMUX)
    if decision.dropped_audio:
        reasons.append(AUDIO_DROP)
    delta, known = estimate_delta(pr, decision)
    return LibraryRow(
        path=path,
        app=app,
        root=root,
        name=os.path.basename(path),
        size_bytes=st.st_size,
        mtime=st.st_mtime,
        duration_s=pr.duration_s,
        needs_work=int(decision.needs_work),
        reasons=",".join(reasons),
        summary=decision.summary(),
        drop_subs=len(decision.dropped_subs),
        encode_audio=len(decision.audio_encode_tracks),
        drop_audio=len(decision.dropped_audio),
        original_lang=original_lang,
        audio_tracks=len(pr.audio),
        sub_tracks=len(pr.subtitles),
        delta_bytes=delta,
        delta_known=int(known),
        pruned_at=pruned_at,
        error=None,
        scan_version=SCAN_VERSION,
        scanned_at=scanned_at,
    )


def scan(
    services,
    *,
    full: bool = False,
    root: str | None = None,
    probe_fn: Callable[[str], ProbeResult] | None = None,
    on_progress: Callable[[float, str], None] | None = None,
) -> ScanStats:
    """Walk the configured roots and refresh the index.

    ``full`` re-probes everything instead of trusting the (path, mtime) cache. ``root`` narrows
    the walk to one configured root or a subdirectory of one — the way you scan a single show
    without paying for the whole library.
    """
    cfg = services.config
    index = services.library
    audit = services.audit
    # Only pay for the arr lookup when the filter that needs it is on.
    titles = services.titles if cfg.policy.audio_language_filter else None
    probe_fn = probe_fn or _default_probe
    started = _scan_token()
    exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in cfg.library.extensions}
    min_size = cfg.library.min_size_bytes

    pairs = resolve_roots(cfg)
    if root:
        want = Path(root)
        # a subdirectory inherits its parent root's app; an unrelated path is refused rather than
        # guessed at, because the app decides which arr the re-run is addressed to.
        pairs = [(want, app) for base, app in pairs if want == base or _under(want, base)]
        if not pairs:
            raise ValueError(f"{root} is not under a configured library root")
        # A typo'd folder would otherwise walk nothing and report a clean, successful scan of
        # zero files — the failure mode looks identical to "everything here is fine".
        if not want.is_dir():
            raise ValueError(f"{root} does not exist")

    stats = ScanStats(roots=[str(p) for p, _ in pairs])
    if not pairs:
        return stats

    # Two passes: a cheap stat-only walk so the progress bar has a real denominator, then the
    # expensive probing. The walk of a few thousand entries costs a fraction of one probe.
    listing: list[tuple[str, os.stat_result, str, str]] = []
    for base, app in pairs:
        for path, st in iter_files(base, exts, min_size):
            listing.append((path, st, app, str(base)))
    stats.files = len(listing)
    if on_progress:
        on_progress(0.0, f"{stats.files} files to check")

    for i, (path, st, app, base) in enumerate(listing, start=1):
        # The durable pause flag is how a redeploy drains the worker; a multi-minute scan has to
        # honour it or "idle — safe to redeploy" would be a lie for the length of a scan.
        if i % 25 == 0 and services.queue.is_paused():
            stats.stopped = True
            break
        existing = index.get(path)
        if (
            not full
            and existing is not None
            and existing.mtime == st.st_mtime
            and existing.size_bytes == st.st_size
            and existing.error is None
            and existing.scan_version == SCAN_VERSION
        ):
            index.touch(path, started)
            stats.cached += 1
            stats.needs_work += int(bool(existing.needs_work))
        else:
            stats.probed += 1
            try:
                pr = probe_fn(path)
            except Exception as exc:  # unreadable/corrupt file — index the failure, keep going
                log.warning("library scan: probe failed for %s: %s", path, exc)
                stats.errors += 1
                index.upsert(
                    LibraryRow(
                        path=path, app=app, root=base, name=os.path.basename(path),
                        size_bytes=st.st_size, mtime=st.st_mtime, error=str(exc)[:400],
                        scan_version=SCAN_VERSION, scanned_at=started,
                    )
                )
            else:
                info = titles.lookup(path) if titles is not None else None
                original = (
                    titles.original_isos(path, info.title if info else None)
                    if titles is not None
                    else None
                )
                decision = decide(pr, cfg.policy, original_langs=original)
                pruned_at = audit.last_done_at(path, "prune", st.st_mtime)
                index.upsert(
                    _row_for(
                        path, st, app, base, pr, decision, pruned_at, started,
                        original_lang=(info.original_language if info else None),
                    )
                )
                stats.needs_work += int(decision.needs_work)
        if on_progress and (i % 5 == 0 or i == stats.files):
            pct = i / stats.files * 100 if stats.files else 100.0
            on_progress(pct, f"{i}/{stats.files} · {stats.needs_work} need work")

    if not stats.stopped:
        stats.removed = index.prune_missing(stats.roots, started)
    return stats


def _under(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
    except ValueError:
        return False
    return True
