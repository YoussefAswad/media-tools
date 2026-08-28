"""The ``library_files`` table: one row per media file found under a configured root.

Rows are keyed by path and validated by (mtime, size, scan_version), which is what makes a
re-scan cheap — see :mod:`orchestrator.library.scan`.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
import threading
from dataclasses import dataclass

from mediacore.db import add_columns

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

#: columns added after the table's first release — see :func:`mediacore.db.add_columns`.
_LIBRARY_ADDED = {
    "enqueued_at": "TEXT",
    "drop_audio": "INTEGER NOT NULL DEFAULT 0",
    "original_lang": "TEXT",
    "scan_version": "INTEGER NOT NULL DEFAULT 0",
}


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
            add_columns(self.db, "library_files", _LIBRARY_ADDED)
            self.db.commit()

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

