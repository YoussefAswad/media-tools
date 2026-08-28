"""Library index — what the pipeline *would* do to every file already on disk.

The per-file pipeline only ever sees freshly imported files, so anything that landed before the
orchestrator existed (or before a policy change) has never been looked at. This module walks the
configured library roots, probes each file once, and stores the resulting
:class:`~quality_check.prune.PruneDecision` in a queryable table. The Library tab renders that
table; enqueueing is a separate, explicit act.

Scanning never touches a file. It is ``probe`` + ``decide()`` only, and ``decide()`` is pure, so
the index is a dry run by construction — the numbers can be wrong, but nothing gets rewritten
because of them.

Scanning is slow (each file costs an mkvmerge + a few ffprobe calls) and runs on the single
worker thread, so it blocks imports while it runs. Two things keep that bearable: the index is
keyed by (path, mtime) so a re-scan only probes what actually changed, and the loop checks the
durable pause flag every few files so "Pause worker" still drains the worker promptly.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from mediacore.probe import ProbeResult, classify_audio
from mediacore.probe import probe as _default_probe
from quality_check.prune import PruneDecision, decide

log = logging.getLogger(__name__)

#: reason tags stored (comma-separated) in ``library_files.reasons``.
SUBS = "subs"
AUDIO = "audio"
REMUX = "remux"
#: audio tracks dropped by the language filter (distinct from AUDIO, a re-encode).
AUDIO_DROP = "audiolang"
REASONS = (SUBS, AUDIO, REMUX, AUDIO_DROP)

#: derived row states the UI filters by.
NEEDS = "needs"
CLEAN = "clean"
DONE = "done"
ERROR = "error"
QUEUED = "queued"
STATUSES = (NEEDS, CLEAN, DONE, ERROR, QUEUED)

_SORTS = {
    # biggest estimated win first, then largest file — the order you'd actually work through.
    "impact": "needs_work DESC, delta_bytes ASC, size_bytes DESC",
    "size": "size_bytes DESC",
    "name": "name COLLATE NOCASE ASC",
    "scanned": "scanned_at DESC",
}
DEFAULT_SORT = "impact"

#: Bump when the stored decision or the size estimate changes shape. A row computed by an older
#: formula is re-probed by the next ordinary scan instead of being trusted as cache — otherwise a
#: fixed estimator would only reach files that happened to change on disk.
SCAN_VERSION = 4

_SCHEMA = """
CREATE TABLE IF NOT EXISTS library_files (
    path         TEXT PRIMARY KEY,
    app          TEXT NOT NULL,
    root         TEXT NOT NULL,
    name         TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    mtime        REAL,
    duration_s   REAL,
    needs_work   INTEGER NOT NULL DEFAULT 0,
    reasons      TEXT NOT NULL DEFAULT '',
    summary      TEXT,
    drop_subs    INTEGER NOT NULL DEFAULT 0,
    encode_audio INTEGER NOT NULL DEFAULT 0,
    drop_audio   INTEGER NOT NULL DEFAULT 0,
    original_lang TEXT,
    audio_tracks INTEGER NOT NULL DEFAULT 0,
    sub_tracks   INTEGER NOT NULL DEFAULT 0,
    delta_bytes  INTEGER NOT NULL DEFAULT 0,
    delta_known  INTEGER NOT NULL DEFAULT 1,
    pruned_at    TEXT,
    enqueued_at  TEXT,
    error        TEXT,
    scan_version INTEGER NOT NULL DEFAULT 0,
    scanned_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_library_needs ON library_files(needs_work);
CREATE INDEX IF NOT EXISTS idx_library_app ON library_files(app);
CREATE INDEX IF NOT EXISTS idx_library_scanned ON library_files(scanned_at);
"""


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _scan_token() -> str:
    """The per-scan ``scanned_at`` stamp. Microsecond precision, because :meth:`prune_missing`
    compares against it: two scans a second apart would otherwise share a timestamp and the
    second one would decide it had seen nothing."""
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="microseconds")


@dataclass
class LibraryRow:
    path: str
    app: str
    root: str
    name: str
    size_bytes: int = 0
    mtime: float | None = None
    duration_s: float | None = None
    needs_work: int = 0
    reasons: str = ""
    summary: str | None = None
    drop_subs: int = 0
    encode_audio: int = 0
    drop_audio: int = 0
    original_lang: str | None = None
    audio_tracks: int = 0
    sub_tracks: int = 0
    delta_bytes: int = 0
    delta_known: int = 1
    pruned_at: str | None = None
    enqueued_at: str | None = None
    error: str | None = None
    scan_version: int = 0
    scanned_at: str = ""

    @property
    def reason_list(self) -> list[str]:
        return [r for r in self.reasons.split(",") if r]

    @property
    def status(self) -> str:
        """The single state the UI badges this row with (most specific first)."""
        if self.error:
            return ERROR
        if self.needs_work:
            return QUEUED if self.enqueued_at else NEEDS
        return DONE if self.pruned_at else CLEAN


class LibraryIndex:
    """The ``library_files`` table: one row per media file found under a configured root."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self._lock = threading.Lock()
        with self._lock:
            self.db.executescript(_SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the table's first creation (idempotent)."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(library_files)")}
        if "enqueued_at" not in cols:
            self.db.execute("ALTER TABLE library_files ADD COLUMN enqueued_at TEXT")
        if "drop_audio" not in cols:
            self.db.execute(
                "ALTER TABLE library_files ADD COLUMN drop_audio INTEGER NOT NULL DEFAULT 0"
            )
        if "original_lang" not in cols:
            self.db.execute("ALTER TABLE library_files ADD COLUMN original_lang TEXT")
        if "scan_version" not in cols:
            self.db.execute(
                "ALTER TABLE library_files ADD COLUMN scan_version INTEGER NOT NULL DEFAULT 0"
            )

    # --- writes ---------------------------------------------------------------------
    def upsert(self, row: LibraryRow) -> None:
        """Replace this file's entry. Keeps ``enqueued_at`` only while the file is unchanged —
        a re-probed file is a new question, so a stale "queued" badge must not survive it."""
        with self._lock:
            self.db.execute(
                "INSERT INTO library_files (path, app, root, name, size_bytes, mtime, duration_s,"
                " needs_work, reasons, summary, drop_subs, encode_audio, drop_audio,"
                " original_lang, audio_tracks, sub_tracks,"
                " delta_bytes, delta_known, pruned_at, enqueued_at, error, scan_version,"
                " scanned_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET"
                "  app=excluded.app, root=excluded.root, name=excluded.name,"
                "  size_bytes=excluded.size_bytes, mtime=excluded.mtime,"
                "  duration_s=excluded.duration_s, needs_work=excluded.needs_work,"
                "  reasons=excluded.reasons, summary=excluded.summary,"
                "  drop_subs=excluded.drop_subs, encode_audio=excluded.encode_audio,"
                "  drop_audio=excluded.drop_audio, original_lang=excluded.original_lang,"
                "  audio_tracks=excluded.audio_tracks, sub_tracks=excluded.sub_tracks,"
                "  delta_bytes=excluded.delta_bytes, delta_known=excluded.delta_known,"
                "  pruned_at=excluded.pruned_at, enqueued_at=excluded.enqueued_at,"
                "  error=excluded.error, scan_version=excluded.scan_version,"
                "  scanned_at=excluded.scanned_at",
                (
                    row.path, row.app, row.root, row.name, row.size_bytes, row.mtime,
                    row.duration_s, row.needs_work, row.reasons, row.summary, row.drop_subs,
                    row.encode_audio, row.drop_audio, row.original_lang,
                    row.audio_tracks, row.sub_tracks, row.delta_bytes,
                    row.delta_known, row.pruned_at, row.enqueued_at, row.error,
                    row.scan_version, row.scanned_at or _now(),
                ),
            )
            self.db.commit()

    def touch(self, path: str, scanned_at: str) -> None:
        """Mark an unchanged (cached) file as seen by this scan, so it survives
        :meth:`prune_missing`."""
        with self._lock:
            self.db.execute(
                "UPDATE library_files SET scanned_at=? WHERE path=?", (scanned_at, path)
            )
            self.db.commit()

    def mark_enqueued(self, paths: list[str], when: str | None = None) -> int:
        if not paths:
            return 0
        ts = when or _now()
        marks = ",".join("?" * len(paths))
        with self._lock:
            cur = self.db.execute(
                f"UPDATE library_files SET enqueued_at=? WHERE path IN ({marks})", (ts, *paths)
            )
            self.db.commit()
            return cur.rowcount

    def mark_clean(
        self,
        path: str,
        *,
        mtime: float,
        size_bytes: int,
        summary: str,
        pruned_at: str | None = None,
    ) -> bool:
        """Record that the pipeline has just finished with this file: nothing left to do, and no
        longer queued. mkvprune is idempotent for these rules, so a completed prune means the
        decision is settled — this saves a re-probe and keeps the table honest between scans."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE library_files SET needs_work=0, reasons='', summary=?, drop_subs=0,"
                " encode_audio=0, drop_audio=0, delta_bytes=0, delta_known=1, mtime=?, size_bytes=?,"
                " pruned_at=COALESCE(?, pruned_at), enqueued_at=NULL, error=NULL WHERE path=?",
                (summary, mtime, size_bytes, pruned_at, path),
            )
            self.db.commit()
            return cur.rowcount > 0

    def forget(self, path: str) -> bool:
        """Drop one row — the file it described is gone (renamed, remuxed to a new extension)."""
        with self._lock:
            cur = self.db.execute("DELETE FROM library_files WHERE path=?", (path,))
            self.db.commit()
            return cur.rowcount > 0

    def reconcile_enqueued(self, active_paths: set[str]) -> int:
        """Clear the "queued" flag on rows whose job is no longer in the queue.

        ``enqueued_at`` is a *cache* of "this file has an active job" — stored so the table can
        filter and count in SQL rather than cross-referencing the queue per row. A job the user
        cancels, deletes or lets dead-letter leaves the queue without ever calling
        :meth:`mark_clean`, so the flag has to be re-derived from the live queue on render or the
        badge (and the "Queued for re-run" tile) sticks forever.
        """
        with self._lock:
            if active_paths:
                marks = ",".join("?" * len(active_paths))
                cur = self.db.execute(
                    "UPDATE library_files SET enqueued_at=NULL"
                    f" WHERE enqueued_at IS NOT NULL AND path NOT IN ({marks})",
                    tuple(active_paths),
                )
            else:
                cur = self.db.execute(
                    "UPDATE library_files SET enqueued_at=NULL WHERE enqueued_at IS NOT NULL"
                )
            self.db.commit()
            return cur.rowcount

    def prune_missing(self, roots: list[str], before: str) -> int:
        """Drop rows under ``roots`` that this scan didn't see (the file is gone). Scoped to the
        roots actually walked, so scanning one folder never evicts the rest of the index."""
        if not roots:
            return 0
        clause = " OR ".join(["path LIKE ?"] * len(roots))
        params = [f"{r.rstrip('/')}/%" for r in roots]
        with self._lock:
            cur = self.db.execute(
                f"DELETE FROM library_files WHERE scanned_at < ? AND ({clause})",
                (before, *params),
            )
            self.db.commit()
            return cur.rowcount

    def clear(self) -> int:
        with self._lock:
            cur = self.db.execute("DELETE FROM library_files")
            self.db.commit()
            return cur.rowcount

    # --- reads ----------------------------------------------------------------------
    def get(self, path: str) -> LibraryRow | None:
        r = self.db.execute("SELECT * FROM library_files WHERE path=?", (path,)).fetchone()
        return LibraryRow(**dict(r)) if r else None

    def _where(
        self,
        *,
        q: str | None,
        app: str | None,
        status: str | None,
        reason: str | None,
        root: str | None,
    ) -> tuple[str, list]:
        where: list[str] = []
        params: list = []
        if q:
            where.append("path LIKE ?")
            params.append(f"%{q}%")
        if app in ("radarr", "sonarr"):
            where.append("app = ?")
            params.append(app)
        if root:
            where.append("path LIKE ?")
            params.append(f"{root.rstrip('/')}/%")
        if reason in REASONS:
            # reasons is a short csv of fixed tags; ','||reasons||',' makes the match unambiguous.
            where.append("(',' || reasons || ',') LIKE ?")
            params.append(f"%,{reason},%")
        if status == NEEDS:
            where.append("needs_work = 1 AND error IS NULL")
        elif status == QUEUED:
            where.append("needs_work = 1 AND enqueued_at IS NOT NULL")
        elif status == CLEAN:
            where.append("needs_work = 0 AND error IS NULL AND pruned_at IS NULL")
        elif status == DONE:
            where.append("needs_work = 0 AND error IS NULL AND pruned_at IS NOT NULL")
        elif status == ERROR:
            where.append("error IS NOT NULL")
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def list(
        self,
        *,
        q: str | None = None,
        app: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        root: str | None = None,
        sort: str = DEFAULT_SORT,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[LibraryRow], int]:
        """A page of rows plus the total number matching the same filters."""
        clause, params = self._where(q=q, app=app, status=status, reason=reason, root=root)
        total = self.db.execute(
            f"SELECT COUNT(*) c FROM library_files{clause}", params
        ).fetchone()["c"]
        order = _SORTS.get(sort, _SORTS[DEFAULT_SORT])
        rows = self.db.execute(
            f"SELECT * FROM library_files{clause} ORDER BY {order}, path ASC LIMIT ? OFFSET ?",
            (*params, limit, max(0, offset)),
        ).fetchall()
        return [LibraryRow(**dict(r)) for r in rows], int(total)

    def paths_matching(
        self,
        *,
        q: str | None = None,
        app: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        root: str | None = None,
        limit: int = 200,
    ) -> list[tuple[str, str]]:
        """(path, app) for every row matching the filters — the 'select all matching' target.
        Only rows that actually need work are ever returned; re-running a clean file is a no-op
        that would still cost a probe."""
        clause, params = self._where(q=q, app=app, status=status, reason=reason, root=root)
        clause = f"{clause} AND needs_work = 1" if clause else " WHERE needs_work = 1"
        order = _SORTS[DEFAULT_SORT]
        rows = self.db.execute(
            f"SELECT path, app FROM library_files{clause} ORDER BY {order}, path ASC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [(r["path"], r["app"]) for r in rows]

    def actionable_totals(
        self,
        *,
        q: str | None = None,
        app: str | None = None,
        status: str | None = None,
        reason: str | None = None,
        root: str | None = None,
    ) -> dict[str, int]:
        """What "select all matching" would actually queue, for the same filters the table shows.

        Scoped to ``needs_work`` rows exactly like :meth:`paths_matching`, so the number the
        confirmation quotes is the number of files that will be rewritten — not the row count,
        which includes clean and errored files the bulk enqueue skips.
        """
        clause, params = self._where(q=q, app=app, status=status, reason=reason, root=root)
        clause = f"{clause} AND needs_work = 1" if clause else " WHERE needs_work = 1"
        r = self.db.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(delta_bytes),0) delta,"
            " COALESCE(SUM(CASE WHEN delta_known=0 THEN 1 ELSE 0 END),0) unknown"
            f" FROM library_files{clause}",
            params,
        ).fetchone()
        return {"count": int(r["n"]), "delta": int(r["delta"]), "unknown": int(r["unknown"])}

    def counts(self, *, app: str | None = None) -> dict[str, int]:
        """Headline totals for the summary tiles (whole index, ignoring the row filters)."""
        clause, params = self._where(q=None, app=app, status=None, reason=None, root=None)
        r = self.db.execute(
            "SELECT COUNT(*) total,"
            " COALESCE(SUM(needs_work),0) needs,"
            " COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END),0) errors,"
            " COALESCE(SUM(CASE WHEN needs_work=1 AND enqueued_at IS NOT NULL THEN 1 ELSE 0 END),0)"
            "   queued,"
            " COALESCE(SUM(CASE WHEN needs_work=1 AND drop_subs>0 THEN 1 ELSE 0 END),0) subs,"
            " COALESCE(SUM(CASE WHEN needs_work=1 AND encode_audio>0 THEN 1 ELSE 0 END),0) audio,"
            " COALESCE(SUM(CASE WHEN needs_work=1 AND drop_audio>0 THEN 1 ELSE 0 END),0) audiodrop,"
            " COALESCE(SUM(CASE WHEN needs_work=1 THEN delta_bytes ELSE 0 END),0) delta,"
            " COALESCE(SUM(CASE WHEN needs_work=1 AND delta_known=0 THEN 1 ELSE 0 END),0)"
            "   delta_unknown,"
            " COALESCE(SUM(size_bytes),0) size,"
            " MAX(scanned_at) last_scan"
            f" FROM library_files{clause}",
            params,
        ).fetchone()
        return {
            "total": int(r["total"]),
            "needs": int(r["needs"]),
            "errors": int(r["errors"]),
            "queued": int(r["queued"]),
            "subs": int(r["subs"]),
            "audio": int(r["audio"]),
            "audiodrop": int(r["audiodrop"]),
            "delta": int(r["delta"]),
            "delta_unknown": int(r["delta_unknown"]),
            "size": int(r["size"]),
            "last_scan": r["last_scan"],
        }


# --- the scan -----------------------------------------------------------------------------


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


def _as_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def track_sizes(pr: ProbeResult) -> dict[int, int]:
    """ffprobe stream index → that track's size in bytes, for the tracks that reveal it.

    Matroska has no per-track bitrate header, so ffprobe's own ``bit_rate`` is empty on the vast
    majority of an MKV library (measured on this one: 13% of audio tracks). What it *does* carry
    is mkvmerge's statistics tags — ``NUMBER_OF_BYTES`` (the exact track size) and ``BPS`` — on
    72% of audio and 86% of subtitle tracks. Reading them is the difference between a size
    estimate that means something and a column of question marks.

    Tried in order: the exact byte count, then a measured average bitrate over the runtime, then
    ffprobe's own bitrate. ``None`` when a track reveals none of them.
    """
    duration = pr.duration_s or 0.0
    sizes: dict[int, int] = {}
    for stream in (pr.ffprobe_json or {}).get("streams", []):
        index = _as_int(stream.get("index"))
        if index is None:
            continue
        tags = {k.upper(): v for k, v in (stream.get("tags") or {}).items()}
        exact = _as_int(tags.get("NUMBER_OF_BYTES"))
        if exact is not None:
            sizes[index] = exact
            continue
        if duration <= 0:
            continue
        bps = _as_int(tags.get("BPS")) or _as_int(stream.get("bit_rate"))
        if bps:
            sizes[index] = int(bps / 8 * duration)
    return sizes


def estimate_delta(pr: ProbeResult, decision: PruneDecision) -> tuple[int, bool]:
    """Estimated size change in bytes (negative = space reclaimed), and whether every part of it
    was actually computable.

    Dropping a subtitle or audio track reclaims exactly that track's bytes. Re-encoding audio
    source track for an AC-3 one at a known bitrate — except the >6-channel *compatibility* path,
    which keeps the original and **adds** a downmix, so its contribution is positive.

    A file where some tracks report a size and others don't yields a partial figure with
    ``known=False``: a floor, not a fiction. Never round an unknown down to a confident zero.
    """
    duration = pr.duration_s or 0.0
    sizes = track_sizes(pr)
    delta = 0
    known = True

    for track in [*decision.dropped_subs, *decision.dropped_audio]:
        size = sizes.get(track.ffprobe_index)
        if size is None:
            known = False
        else:
            delta -= size

    for track in decision.audio_encode_tracks:
        action = classify_audio(track)
        if not action.bitrate_bps or duration <= 0:
            known = False
            continue
        added = int(action.bitrate_bps / 8 * duration)
        if action.kind == "compat":
            delta += added  # the original stays; a 5.1 AC-3 downmix is added alongside it
            continue
        source = sizes.get(track.ffprobe_index)
        if source is None:
            known = False
            continue
        delta += added - source

    return delta, known


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
