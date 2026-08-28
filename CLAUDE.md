# media-orchestrator

A **uv-workspace monorepo** built around one product: the **FastAPI media orchestrator**. It
receives Radarr/Sonarr webhooks, runs a per-file pipeline, indexes the back catalogue, and
monitors the host. `mkvprune` stays an independent, vendored tool invoked as a subprocess;
everything else the service needs lives in the shared `mediacore` library.

## What this project is for (goals)

The overarching goal was to automate post-import media processing that used to be a bash Custom
Script plus a standalone renamer, and to unify three tools that had drifted apart:

- **mkvprune** — remux an MKV to drop unwanted tracks and re-encode audio that isn't already
  Dolby/DTS, while keeping DV/HDR10(+), track flags, chapters, and attachments **bit-exact**.
- **mediacore.policy** (was the `quality-check` package) — probe a file and decide what mkvprune
  should prune (foreign subtitles, non-Dolby/DTS multichannel audio, too many audio tracks,
  non-original-language audio) and flag findings to ntfy.
- **mediacore.renumber** (was the `seasonal-shift` package) — renumber anime episodes on disk
  (seasonal ⇄ absolute); the orchestrator re-registers the file with Sonarr under its original
  episode.
- **orchestrator** — the glue: webhook ingest → durable job queue → per-file pipeline → audit log
  → ntfy status → a small web UI (job queue, audit actions, a downloads/bandwidth ledger, a
  library index for the back catalogue, config editor).

All of that is **live**: deployed as a Docker container in the sibling `my-media-server` compose
project, replacing the old bash Custom Script and standalone seasonal-shift.

## Layout

```
packages/
  mkvprune/        pure-stdlib remux/encode CLI (NO network, NO deps) — invoked as a subprocess
  mediacore/       everything the service shares:
                     config, models, db (shared conn + migrations), audit, paths, probe facade
                     arr/       ARR client + webhook event parsing + language table
                     policy/    ffprobe-based rules + prune decision (builds mkvprune argv)
                     renumber/  SxxExx planner/executor for seasonal <-> absolute shifts
                     ntfy, qbit, jellyfin, dashdot, docker_status, scrutiny clients
  orchestrator/    FastAPI app, job queue+worker, dispatch pipeline, library index, ledger, web UI
deploy/            Dockerfile, compose fragment, config.example.yaml, README
```

`quality-check` and `seasonal-shift` used to be separate packages. They were absorbed into
`mediacore` as `policy/` and `renumber/`: the orchestrator only ever imported 4 of
seasonal-shift's 13 modules, its `sonarr.py` duplicated `mediacore.arr.client`, and their typer/
rich/watchdog CLI dependencies were being installed into the production image for nothing.
`decide()` also needs the arr's original-language metadata now, which a standalone CLI can't
supply. Their tests live in `packages/mediacore/tests/`.

The pipeline is an ordered **registry of steps** (`orchestrator/steps/`), run by `Pipeline` in
`orchestrator/dispatch.py`. It runs, per file, in this order — **the order matters**:

1. **ledger capture** (bandwidth from qBittorrent) — FIRST, so consumed bandwidth is recorded even
   if a later step fails or the torrent is later deleted. Idempotent upsert by infohash.
2. **quality-check** (`mediacore.policy`) — probe + findings → ntfy.
3. **prune** (mkvprune) — replaces the file in place; streams live progress into the job row.
4. **seasonal-shift** (`mediacore.renumber`) — moves/re-imports (Sonarr only); runs AFTER prune so it operates on a stable
   path.
5. **Jellyfin refresh** — best-effort, after prune ran (ok or skip).
6. **status rollup** — ntfy.

**Adding a pipeline step** = one class in `orchestrator/steps/`, one entry in
`steps.STEP_TYPES`, and its name in `pipeline.steps` in the config. A step implements `name`, `silent`, `applies(ctx)` and `run(ctx)`
(see `steps/base.py`). The *framework* owns timing, the audit record, the per-action ntfy ping,
and following a file the step moved — a step never does those itself. Three rules worth knowing:

- **Return `FAIL` to continue, `raise` to fail the job.** A returned FAIL is recorded and the
  pipeline moves on (a quality-check probe failure still lets prune try). Raising `DispatchError`
  fails the job for retry; raising `InsufficientSpaceError` defers it without consuming an attempt.
- **`silent = True` means audit-only** — recorded, but kept out of the ntfy rollup *and* out of
  the job row's current-step. The ledger snapshot and the Jellyfin refresh are bookkeeping about
  a job, not work done to the file.
- **A step that moves the file returns `StepResult.new_path`**; the pipeline follows it so later
  steps and the job's final path see the new location. Don't mutate `ctx.local_path`.

**The pipeline IS `pipeline.steps` in the config** — an ordered list of names. A step runs
because it is in that list, which is why there are no `enable_*` booleans any more; a name not in
`STEP_TYPES` is a startup validation error rather than a silently dropped step. A step therefore
asks `ctx.steps` (the names in this pipeline), not a config flag, when it needs to know whether
another step is configured at all — that is how the Jellyfin refresh tells "prune hasn't run yet"
from "prune isn't in the pipeline".

`Dispatcher` routes on job *kind* before any of that: `arr_event`/`rerun` run the per-file
pipeline, `library_scan` is its own handler (no event, no file to change).

That pipeline only ever sees files at import. The **Library tab** (`orchestrator/library/`) is
the back-catalogue path: a `library_scan` job walks the configured roots, probes each file once and
stores what `decide()` *would* do in a `library_files` table; the tab renders that table with
filters, and queueing any subset of it is a separate click that enqueues the same `rerun` payload
`/rerun` already uses. Scanning never writes to a media file — it is probe + `decide()`, and
`decide()` is pure, so the index is a dry run by construction.

## Running, testing, linting

- **uv is not on `$PATH`.** Always use `~/.local/bin/uv run --python 3.12 ...`.
- **Tests:** one run covers the whole workspace (`testpaths = ["packages"]`):
  ```
  ~/.local/bin/uv run --python 3.12 pytest
  ```
  (Test-module basenames are unique across packages and `conftest.py` resolves per-directory, so
  there is no collection clash. An older note here claimed otherwise — it was stale.)
- **Lint:** `~/.local/bin/uv run --python 3.12 ruff check packages` (this DOES work across all
  packages). Config lives in the root `pyproject.toml`; `UP047`/`B008` are intentionally ignored.
- **If a source edit seems to have no effect, clear `__pycache__`.** Bytecode here is
  timestamp-invalidated on (mtime, size): an edit that keeps the file the same length and lands in
  the same second as the last compile is not detected, and the old bytecode keeps running while
  the file on disk reads correct. `inspect.getsource` shows the *file*, so it lies too — `dis` on
  the function is what settles it. `find packages -name __pycache__ -prune -exec rm -rf {} +`.
- Pyright "Import could not be resolved" warnings in the editor are just the analyzer's path config
  (workspace packages aren't on its resolution path) — **not real errors**; the code runs fine.

## Conventions

- **Best-effort clients never raise.** `Notifier`, `QbitClient`, `JellyfinClient` log and return
  `None`/`False` on any failure. A failed notification/refresh/snapshot must not fail a job.
- **mkvprune stays pure.** Standard library only, no network, no third-party deps. It's invoked as
  a CLI subprocess by the orchestrator, never imported for side effects. Keep it that way.
- **mkvprune always sets the first track (per type) as default.**
- **Config is grouped by job**: connections (`arr`, `ntfy`, `qbit`, `jellyfin`), media policy
  (`policy`), operational tuning (`worker`, `pipeline`), plus `library`, `monitoring`, `server`,
  `web`, `audit`, `shows`. Fifteen scalars used to sit loose at the root mixing all three; they
  are **still accepted there** (`OrchestratorConfig._lift_legacy_root_keys` lifts them, and
  `enable_x: false` drops that step from `pipeline.steps`), because nothing rewrites a user's
  file. An explicit new-style section always wins over a legacy key.
- **Config = Pydantic v2 + YAML + `${ENV}` / `${VAR:-default}` expansion.** Secrets stay as `${VAR}`
  references in the YAML (expanded at load, never written back resolved) so the config file is safe
  to edit in the web UI. Env vars are injected by compose from `.env`.
- **Audit = JSONL (source of truth) + SQLite mirror (queryable index).** One SQLite file
  (`/state/orchestrator.db`, WAL) holds jobs, actions, downloads (ledger), and cutoffs.
- **Web UI is server-rendered** (Jinja2) with vanilla-JS `data-src` polling. **No vendored CDN
  libraries** (CSP-safe). Theme-aware CSS. Byte values via a context-aware `bytes` Jinja filter.
  One router per tab in `web/routes/`; shared template env + auth in `web/deps.py` (use
  `page()` / `partial()`, not `TemplateResponse`); domain-to-template translation in
  `web/presenters.py`. **The `data-*` panel contract is documented in
  `packages/orchestrator/README.md`** — adding a filter dimension needs no JavaScript, just a
  `.filters` group named in the panel's `data-chip-filters`. Summary tiles go through
  `_summary.html`, which takes dicts (`value`, `label`, `tone`, `title`, `hide_when_zero`, …);
  don't fork it. `static/` is `poll.js` (the engine) + one file per page that needs more.
- **Schema migrations go through one helper.** Each table module declares a module-level
  `_<TABLE>_ADDED` map of *columns added after its first release* (`{"progress": "REAL", ...}`)
  and calls `mediacore.db.add_columns(db, "<table>", _<TABLE>_ADDED)` right after its
  `executescript(_SCHEMA)`. It's idempotent and returns what it added. Four tables used to
  hand-roll the same `PRAGMA table_info` + guarded `ALTER TABLE ADD COLUMN` loop; at nine tables
  that was duplicated infrastructure whose failure mode (a forgotten guard) only shows up on
  somebody else's older db. **Any `NOT NULL` column MUST carry a `DEFAULT`** — SQLite accepts a
  bare `NOT NULL` on an *empty* table and rejects it on a populated one, so the mistake passes in
  dev/CI and fails at startup in production. `test_db.py` asserts this across every map.
  Small runtime key/values (e.g. the durable pause flag, last-vacuum date) live in the `meta`
  table via `JobQueue.get_meta/set_meta`.

## Deploy

- Image `youssefaswad/media-orchestrator` on Docker Hub. Build:
  `docker build -f deploy/orchestrator/Dockerfile -t youssefaswad/media-orchestrator:<ver> -t youssefaswad/media-orchestrator:latest .`
  then `docker push` both tags. Bump a semver tag per change; `latest` is what compose pulls.
- **Entrypoint is the venv console script `orchestrator`** (not `uv run`), so it works as non-root
  (UID 1000).
- Runs in the sibling `my-media-server` compose project. Only `/config`, `/state`, `/data` are
  bind-mounted — **static assets (CSS/JS/templates) are baked into the image**, so any UI change
  needs a rebuild + `docker compose up -d media-orchestrator`, and viewers must hard-refresh
  (cached `app.js`/`app.css`).
- Secrets in `my-media-server/.env` (gitignored; `.env.enc` is the SOPS+age-encrypted tracked copy
  — re-encrypt with `sops --encrypt --input-type dotenv --output-type dotenv` after any change).
- ARR points at it via a **Webhook** Connect (header `X-Webhook-Token: ${ORCH_WEBHOOK_SECRET}`,
  triggers On Import/Upgrade/Rename).

## Pitfalls to avoid (hard-won)

1. **Never recreate the container while a job is processing.** A running job means an in-flight
   mkvprune (mkvmerge/ffmpeg) doing long work. Killing it mid-mux wastes it (and used to strand a
   `*.tmp.mkv`; that now self-heals, but it's still wasteful and interrupts imports). Before any
   redeploy, confirm idle: no `processing`/`queued` rows in `jobs` and no `mkv`/`ffmpeg` process in
   the container. Building the image and committing are fine any time — only the **recreate** waits.
   The dashboard now has a **Pause worker** button (durable, stored in the `meta` table): pause
   stops claiming new jobs while letting the in-flight one finish, and the toolbar shows
   "idle — safe to redeploy" once the queue is drained. `/readyz` (used by the container
   healthcheck) reports the worker loop's real liveness via a heartbeat; a long prune keeps it
   `_busy` so a legitimate multi-minute mux does not read as unhealthy. `/healthz` is liveness-only.

2. **The shared SQLite connection must be accessed serially.** The web threadpool and the worker
   share one `sqlite3.Connection`; concurrent stepping of the same connection intermittently
   returns truncated/`None` rows (symptom seen: `TypeError: 'NoneType' object is not subscriptable`
   500s, and the Downloads "remaining" card vanishing). Fixed by `mediacore.db.open_db` returning a
   `_SerializedConnection` subclass that runs+buffers every statement under one lock. **Don't add
   raw `db.execute` paths that bypass this** — go through `open_db`.

3. **Never merge ffmpeg `-progress` (stdout) with `-v error` (stderr) into one pipe.** They
   interleave at the byte level; on a **TrueHD** track (which emits many benign "non-monotonic DTS"
   muxer warnings) the interleaving mangles error lines so the benign-pattern filter misses them,
   causing **false verification failures (exit 3)** on a perfectly good remux. Use
   `proc.run_streamed_split` (stdout streamed for progress, stderr drained on its own pipe) and scan
   only the clean stderr. This is why `mkvprune/verify.py` keeps the streams separate.

   **The benign-DTS chatter is not TrueHD-only, and its companion line counts too.** A
   stream-copied **PGS subtitle** track produces the identical "non monotonically increasing dts"
   flood, and ffmpeg collapses consecutive duplicates into its own `Last message repeated N times`
   line. That line is a statement *about* the message above it, never a defect, so it has to be
   filtered alongside the patterns it collapses — filtering the DTS lines but keeping their
   companions dead-lettered 30 real files (Bleach S01E190-E223) at exit 3 on a remux ffmpeg itself
   exited **0** on: 1094 stderr lines were 726 DTS warnings + 368 repeat lines + nothing else.
   A genuinely repeated error still prints its own first occurrence, which stays fatal.

4. **qBittorrent (LSIO v5.2.3) WebUI login returns HTTP 204, not 200 "Ok.".** And it has
   `HostHeaderValidation=true`, so the client must send a `Referer`. Don't treat non-200 as failure.

5. **ntfy request headers are latin-1.** A UTF-8 title (em-dash, CJK, emoji) raises
   `UnicodeEncodeError` in `requests`. `Notifier` latin-1-sanitizes header values and catches
   `Exception` (not just `RequestException`). Keep labels ASCII-hyphenated.

6. **Renumbering re-imports by the ORIGINAL episode number**, not the shifted one. In
   `mediacore.renumber.models.FileOperation`, `season`/`episode` are parsed from the source
   filename; only `destination` carries the shifted numbers. Sonarr tracks the episode by its
   original numbering. Known limits: `EPISODE_PATTERN` (now in `mediacore.renumber.planner`) is
   2-digit `SxxExx` only — multi-episode files (`S01E01-E02`) and >99-episode seasons don't match
   and are skipped.

7. **GB vs GiB are different magnitudes, and both are stored as raw bytes.** `human_bytes(n, iec=)`
   formats decimal (÷1000, "GB") or binary (÷1024, "GiB"); `parse_bytes` matches the suffix. The
   Downloads UI has a per-browser toggle (`localStorage`). qBittorrent itself counts in GiB, so the
   GiB view matches what qBittorrent shows.

8. **Caddyfile changes need `docker compose up -d --force-recreate caddy`** (a reload isn't enough)
   — relevant when touching the orchestrator's Caddy route in `my-media-server`.

9. **Media-classification rule changes must be measured against the real library** (ffprobe hit
   rates) before being trusted — don't ship a rule on intuition.

10. **A job stuck at `queued` may be intentionally deferred, not broken.** Two paths set a future
    `not_before` on a `queued` row (so `claim_next` skips it until then): retry backoff after a
    failure (`retry_backoff_s`), and the low-disk-space guard, which `defer()`s the prune
    **without** consuming an attempt so it never dead-letters for space alone — it waits (default
    30 min) for disk to free up. So a `queued` job that isn't being picked up is expected while its
    `not_before` is in the future; check that column before assuming the worker is wedged. The
    worker also runs periodic maintenance (retention prune of jobs+actions >`retention_days`,
    bounded dead-letter auto-revive, quota-usage ntfy alerts) on its own thread between claims.

11. **Monitoring metrics come from other containers, not the orchestrator's own namespace.** The
    Monitoring tab (`monitoring.*` config) reads external best-effort clients in `mediacore`
    (`docker_status`, `dashdot`, `scrutiny`). Each is wrapped by a **source** in
    `orchestrator/monitor/` that declares its own `name`, `enabled` and `interval_s` and knows how
    to `collect()`; the worker loops over `build_sources()` with one generic gate and stores the
    result in `services.snapshots[name]`. **The web routes read the snapshot, never the socket/API
    per request** — `/ui/mon-summary` regressed on exactly this once, calling `containers()` live
    on a partial that polls every 5s. Read it via `services.snapshot(name)`, whose `ok` is
    `None` = never collected / `True` = fine / `False` = unreachable; the shared
    `_source_state.html` macro turns those three plus "switched off" into the panel preamble.
    **Adding a source** = one class in `orchestrator/monitor/`, one entry in `build_sources()`,
    one config block, one partial. Hard-won gotchas when extending it:
    - **dashdot on the compose bridge reports its OWN container's network**, not the host NIC — its
      `network` widget is useless there (CPU/temp/RAM are fine, they come from its `/:/mnt/host`
      mount). Real host throughput needs dashdot in `network_mode: host`; then it loses its compose
      DNS name, so **every bridge consumer must switch to `host.docker.internal`** (needs
      `extra_hosts: ["host.docker.internal:host-gateway"]`) — that's the orchestrator AND the Caddy
      route (`reverse_proxy host.docker.internal:${DASH_PORT}`, then force-recreate caddy per
      pitfall 8). `DASHDOT_PORT` sets its host port so the existing URL/route is preserved.
    - **The Docker socket is `root:docker` (gid 964 on this host)** — the UID-1000 orchestrator
      needs `group_add: ["<gid>"]` to read `/var/run/docker.sock:ro`. Per-container CPU/mem needs
      the stats API (~1s each) so it's fetched in parallel for running containers only.
    - **Scrutiny's `/api/summary` nests devices under `data.summary.<wwn>`** (one level deeper than
      its docs imply). **USB-bridge drives (e.g. Seagate Expansion) usually don't expose SMART** —
      the internal NVMe/SATA disks do; don't expect the USB media disk to appear.
    - **Alert thresholds are host-specific — measure before trusting** (see pitfall 9's spirit): the
      Ryzen 7 5800H's Tctl sensor idles ~82-86°C, so the default 75/85 CPU-temp thresholds page
      constantly; this host runs 92/97. The alert engine (`orchestrator/alerts.py`) debounces
      (`sustain_s`), applies recovery hysteresis (`clear_s`), de-dupes, and throttles reminders
      (`cooldown_s`) — a config file edited on disk is **not** hot-reloaded (only the Config-tab
      save calls `reload_config`); a direct edit needs a container restart/recreate.

12. **The library index is cheap to refresh but expensive to build, and it can lie about
    itself.** A scan costs one mkvmerge + a few ffprobe calls *per file* and runs on the single
    worker, so a full library blocks imports for as long as it takes. Three things keep that
    honest, and all three are load-bearing:
    - Rows are keyed by **(path, mtime, size)**, so a re-scan only re-probes what actually
      changed; a scan that dies halfway resumes almost free. `prune_missing` is scoped to the
      roots that were actually walked and skipped entirely on a partial scan, so scanning one
      folder never evicts the rest of the index.
    - `scanned_at` is stamped with **microsecond** precision for exactly this reason: two scans in
      the same second would otherwise share a timestamp and the second one would conclude it had
      seen nothing (and delete every row it just wrote).
    - The scan polls the durable pause flag every 25 files, so "Pause worker → idle — safe to
      redeploy" stays true instead of waiting out a 40-minute walk.
    A `library.roots` entry's `app` decides which arr a re-run from that tree is addressed to, so
    a root holding both movies and episodes must be listed twice, one subtree each. Bulk enqueue
    resolves every target **through the index** (never straight from the form), which is what
    stops a crafted POST pointing the pipeline at an arbitrary path. And after a prune finishes,
    `Dispatcher._sync_library_row` settles that file's row in place (no re-probe) — without it the
    table would keep showing a pre-prune decision and a stale "queued" badge until the next scan.

13. **Per-track sizes come from mkvmerge's statistics tags, not from ffprobe.** Matroska has no
    per-track bitrate header, so ffprobe's own `bit_rate` is empty on most of an MKV library
    (measured over 40 real files / 67 audio tracks: **13%**). What the files *do* carry is
    mkvmerge's statistics tags — `NUMBER_OF_BYTES` (the exact track size) and `BPS` — present on
    **72%** of audio and **86%** of subtitle tracks. `library.track_sizes` reads them out of the
    retained `ProbeResult.ffprobe_json` (mkvprune stays untouched: this only *reads* json it
    already keeps) and falls back bytes → BPS × runtime → ffprobe bitrate → unknown. Reading only
    `Track.bit_rate` is what made every row's estimate read "unknown" on the first deploy.
    A file where some tracks report a size and others don't yields a **partial** delta flagged
    `known=False` — a floor, not a fiction. Never collapse that into a confident `0`.

14. **The pipeline usually makes files BIGGER on this library, and the UI must not imply
    otherwise.** Measured over a real sample: dropping foreign *text* subs reclaims only ~200 KB
    per file, while re-encoding a 300-400 kbps AAC/Opus track to 640 kbps AC-3 **adds** 50-100 MB.
    Net over 14 files that needed work: **+379 MB**. That is correct behaviour — the AC-3
    re-encode buys player compatibility, not space — but it means "estimated saving" is the wrong
    frame. Keep the label neutral ("Est. size change", "reclaims"/"adds", green vs amber) rather
    than assuming a prune shrinks anything. Image subs (PGS/VobSub) are the exception: those run
    to megabytes each.

15. **`library_files.enqueued_at` is a cache of "this file has an active job", so it must be
    re-derived, not just written.** It is stamped at enqueue time so the table can filter and
    count in SQL, but cancelling a job, deleting it, or letting it dead-letter never calls
    `mark_clean` — the flag would stick and the "Queued for re-run" tile would over-count forever
    (it did). Both Library routes call `_sync_queued_flags`, which walks the live queue once
    (`parse_event(...).primary_path()` → `to_local`) and clears every flag with no matching
    active job. Anything that adds a new way for a job to leave the queue needs nothing extra —
    that is the point of deriving it.

16. **A changed decision/estimate formula needs `library.index.SCAN_VERSION` bumped.** That is
    the whole reason `library/estimate.py` is its own module — the size model is the thing most
    likely to change, and the bump is what makes the change reach cached rows. The scan skips
    files whose (path, mtime, size) is unchanged, so a fixed estimator would otherwise only ever
    reach files that happened to change on disk — the whole index would keep serving numbers from
    the old formula. Rows stamped with an older version are re-probed by the next *ordinary*
    scan, no "re-probe unchanged files" needed.

17. **"Original language" comes from the arr, and every unknown must widen the keep set, not
    narrow it.** The audio language filter keeps `audio_keep_langs` plus the title's original
    language, resolved from Radarr/Sonarr's `originalLanguage` — already present in the webhook
    payload for both apps (no API call at import), and looked up by longest-prefix path match
    (`orchestrator/titles.py`) for the paths that have no event: the library scan, a Library-tab
    re-run, a manual `/rerun`. Measured on the real library: 244/244 titles carry a usable
    `originalLanguage`, and path matching resolves 2300/2303 files (99.9% — the misses are folders
    the arr no longer knows about).
    Four rails, each needed by a real file, applied in this order:
    - **`audio_keep_original` with no original resolved keeps every track.** This is the one that
      bites: falling through to "filter on eng/ara alone" strips the Japanese track off every
      anime the moment an arr is briefly unreachable. The test asserts the filter *never ran*, not
      merely that nothing was dropped — the never-empty rail would otherwise mask the bug (it did).
    - **Untagged (`und`/`mis`/`zxx`) tracks are kept.** 0.5% of this library has one; the single
      audio track of *The Iron Giant* is `und`, and dropping it leaves a silent film.
    - **A selection that would drop every audio track is abandoned wholesale**, so `audio_arg` can
      never be `"none"`. A file matching nothing means the policy is wrong about that file, and
      silence is never the better answer.
    - **Per-title overrides** (`audio_original_overrides`) for metadata that is *correct but not
      what you want*: TVDb rightly calls *Star Wars: Visions* English-original (Lucasfilm), yet its
      Japanese track is the authentic one. No data source fixes this; only an override does.
    Tracks the filter drops are excluded from the AC-3 re-encode set — encoding a track that is
    about to be dropped burns hours for nothing.

18. **A Jellyfin library scan will never notice a file the pipeline rewrote.**
    `POST /Library/Refresh` ("Scan Media Library") is a file-*presence* walk: measured on the real
    2,231-episode library it finishes in **9 seconds** and only reacts to paths appearing, moving
    or disappearing. mkvprune replaces files in place, so Jellyfin keeps serving the track list
    *and the file size* it recorded at import — confirmed on a pruned episode reading 9 audio /
    40 subs / 579 MB while the file on disk was 2 / 4 / 382 MB. The fix is what a human does by
    hand: `POST /Items/{id}/Refresh?metadataRefreshMode=FullRefresh`, which re-probes that one
    item (verified: the same episode corrected itself in a single call). The library scan is kept
    only as the fallback for a path Jellyfin has never seen — a new import, or one the pipeline
    renamed — which is the case it is actually right for.
    Two traps when resolving the path to an item id:
    - **Jellyfin mounts the same media under different prefixes.** Here Radarr/Sonarr and the
      orchestrator see `/data/Movies` and `/data/Shows` while Jellyfin has `/data/movies` and
      `/data/tvshows` (plus separate `/data/shared/*` libraries). `jellyfin.path_map` handles it;
      failing that the client matches trailing path components, and **refuses an ambiguous match**
      rather than refreshing the wrong copy of a title that exists in two libraries.
    - **`/Users` does not return an administrator first.** Building the path index as the first
      user returned 10 of 2,231 episodes, because a non-admin only sees the libraries shared with
      them. Always resolve `Policy.IsAdministrator` before querying `/Items`.

19. **The foreign-subtitle rule has a codec scope, and it dominates the "needs work" count.**
    `policy.subtitle_drop_scope` is `all` (text + image) or `image` (PGS/VobSub only). Measured on
    the real 2,267-file index: **1,270 files need work under `all`, 477 under `image`** — 793 files
    (62%) are flagged solely because they carry foreign *text* subs. That matters because the
    original reason for dropping subs was a TV player that fails on many bitmap subs while
    handling SRT fine, and because text subs reclaim ~200 KB against a full remux of the file.
    Classification checks the ffprobe codec name (`subtitle_image_codecs`) *and* the mkvmerge
    codec id: some tracks report no ffprobe codec name at all (an mp4 `Timed Text` track), and
    classifying by omission would read a bitmap sub as text.
    Note `subtitle_image_codecs` existed in config files for a long time while **no code read it**
    — the rule had stopped gating on codec. It is live again; check a config key is actually a
    model field before assuming it does anything.

20. **A staging dir moves part of the prune's disk cost, it does not remove it — so both volumes
    get checked.** `prune_scratch_dir` (mkvprune's `--scratch-dir`) puts the temp mux and the AC-3
    encodes on fast local storage instead of beside the source; here `/mnt/vault/tmp/mkvprune`
    (NVMe) is bind-mounted to `/scratch`, off the 91%-full media disk. But `replace._place` cannot
    rename across filesystems: it copies the finished mux back into the *destination* dir as a
    `.mkvprune.part` next to the still-present original, then swaps. So a cross-filesystem staging
    dir needs room on **both** volumes, and checking only one would green-light a prune that muxes
    for an hour and then dies at the last step. `steps.prune.PruneStep._ensure_free_space` checks source and
    scratch (collapsing to one check when they share a volume, via `_same_filesystem`), and
    mkvprune adds its own destination precheck (`estimate_placement_bytes`, deliberately smaller
    than the scratch estimate — the encodes never land there).
    Two things that bite:
    - **The stale-temp self-heal has to follow the scratch dir.** It looked for `{stem}.tmp.mkv`
      beside the source; with staging the temp is in the scratch dir, and a killed run would
      otherwise strand a temp that mkvprune refuses to overwrite — failing every future prune of
      that title.
    - **An unreachable scratch dir is a misconfiguration, not a shortfall.** It logs and skips
      rather than raising `InsufficientSpaceError`, because deferring would hide a typo'd path
      behind a 30-minute backoff forever.

## Origin note

mkvprune and seasonal-shift were **copied** (not `git subtree`-imported) from
`/mnt/vault/data/projects/{mkvprune,seasonal-shift}`; the copies here are the source of truth. Git
history of the originals was not preserved. seasonal-shift's reachable core has since been
absorbed into `mediacore.renumber`; the standalone CLI, watcher, doctor, undo and Sonarr client
are gone (see Layout).
