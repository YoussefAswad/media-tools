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
                     ntfy, qbit, jellyfin, dashdot, docker_status, scrutiny, router clients
  orchestrator/    FastAPI app, job queue+worker, dispatch pipeline, library index, ledger,
                   per-device WAN usage, web UI
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
- **mkvprune always sets the first track (per type) as default** — except that an added
  AC-3 compatibility downmix takes the flag on behalf of the >6-channel source it was made
  from (see pitfall 24).
- **Config is grouped by job**: connections (`arr`, `ntfy`, `qbit`, `jellyfin`), media policy
  (`policy`), operational tuning (`worker`, `pipeline`, `notifications`), plus `library`,
  `monitoring`, `server`, `web`, `audit`, `shows`. Fifteen scalars used to sit loose at the root
  mixing all three; they are **still accepted there**
  (`OrchestratorConfig._lift_legacy_root_keys` lifts them, `enable_x: false` drops that step from
  `pipeline.steps`, and `ntfy.status_granularity` maps onto the `notifications` rules), because
  nothing rewrites a user's file. An explicit new-style section always wins over a legacy key.
  Note `ntfy` is the **connection** (url, topic, credentials) and `notifications` is the
  **policy** (what is sent, at what priority, how often) — the same split as `arr` vs `policy`.
- **Every notification goes through `services.notify`** (`orchestrator/notifications.py`), never
  `services.notifier.send` directly. That one seam owns the per-event enable/priority/topic
  override and the `once_per` de-duplication, so **adding a notification** = one `send()` call
  with a new event name, one entry in `notifications.EVENTS`, and one `NotifyRule` field on
  `NotificationsConfig` (a test asserts those stay in step). The emitter still owns what the
  message *says*; the facade owns what happens to it.
- **Config = Pydantic v2 + YAML + `${ENV}` / `${VAR:-default}` expansion.** Secrets stay as `${VAR}`
  references in the YAML (expanded at load, never written back resolved) so the config file is safe
  to edit in the web UI. Env vars are injected by compose from `.env`.
- **Audit = JSONL (source of truth) + SQLite mirror (queryable index).** One SQLite file
  (`/state/orchestrator.db`, WAL) holds jobs, actions, downloads (ledger), per-device WAN
  usage, and quota periods.
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

21. **The staging dir is a 2x speedup, and the reason is spindle contention, not NVMe speed.**
    A remux reads and writes the whole file at once, so with the temp mux beside the source both
    streams hit the same disk. Measured on this host (`/dev/sda2` is a rotational USB Seagate
    Expansion, NTFS via ntfs3, 91% full): 117 MB/s writing alone, but **45.9 MB/s reading and
    writing it at the same time**. Staging splits that into two clean sequential passes.
    A/B on one 6.70 GB source, same argv, byte-identical 6.49 GB output, runs interleaved
    A/B/A/B with a `sync` inside each timing: **229 s median beside the source vs 115 s on the
    NVMe scratch — 2.0x, and the spread tightens from 19 s to 2.5 s.** That matches the 439
    pre-staging production prunes, which held a flat ~40 s/GB from 5 GB to 34 GB (the signature
    of a bottleneck, not of per-file overhead), and the first post-staging one (*The Post*,
    3.17 GB in 57.0 s = 18.0 s/GB).
    Three things to keep straight before quoting the 2x:
    - **It only speeds up the mux, so an encode job gains proportionally less.** *Oppenheimer*
      pruned at 32 s/GB *with* staging because it re-encoded an Opus track to AC-3; that is
      ffmpeg CPU time, which staging does not touch.
    - **It buys wall clock, not media-disk writes.** Per pitfall 20 the cross-filesystem
      `_place` still copies the full output back onto the media disk. The win is
      de-interleaving the writes, not removing them.
    - **Mounting `/scratch` is not enabling it.** The compose mount landed six hours before
      `prune_scratch_dir: /scratch` reached `config.yaml`, and everything pruned in that window
      still staged on the media disk. The mount alone does nothing.
    Worth re-measuring if the media disk is ever moved off USB/ntfs3 — the whole gain is that
    disk's read+write penalty, and a different disk has a different one.

22. **Notifications were the one subsystem with no policy layer, and it showed: 554 messages in
    12 hours, 60% of them redundant.** Measured from ntfy's own message cache before this was
    fixed, and each number picked a different bug:
    - **Every file notified twice, and the second contained the first.** 153 titles got a
      quality-check finding (priority 4) and then the job rollup (priority 3) a median **8
      seconds** later, and **153 of 153** rollups restated the finding text verbatim. You were
      paged about a problem moments before being told it had been fixed automatically. So a
      finding is suppressed while `prune` is in the pipeline (`suppress_findings_when_pruning`),
      and *only* then — with no prune configured the finding is the only thing that tells you,
      which is the rail the test pins.
    - **Retries re-sent everything.** A retry re-runs the whole pipeline under the same job id,
      so 30 files that dead-lettered sent their finding **7 times each** and their death twice
      each: **270 of the 554 messages came from those 30 files**. That is what `once_per: job`
      exists for. Suppression is recorded only for a message that actually *went out*, so an
      unreachable ntfy doesn't silently eat the notification.
    - **The loudest emitter had no switch at all.** `ntfy.status_granularity` gated the rollup
      and the per-step ping and nothing else; findings, library scans, quota and monitoring
      alerts were unconditional. Every emitter now has a `NotifyRule`.
    Two things worth keeping in mind when extending it:
    - **`status_granularity` is load-time only now.** It is mapped onto the new rules by the
      model validator, so setting `cfg.ntfy.status_granularity` on an already-built config does
      nothing (it silently did nothing in three tests before this landed). Set
      `cfg.notifications.*` instead.
    - **Header text is latin-1, and "replace" is not the same as transliterate.** The em dash in
      the orchestrator's own titles became `?` in 88 of those 554 messages
      (`re-run Bleach - S01E202.mkv ? failed`). `notify._TRANSLIT` maps the punctuation that
      actually occurs before the never-raise fallback handles CJK/emoji. Prefer ASCII `-` in new
      titles anyway.

23. **Per-device WAN usage comes from the router, and every other source of that number on this
    host is wrong.** The Bandwidth tab answers "how much internet did the household use, and
    which device used it". Three candidate sources were measured before anything was built, and
    two of them lie:
    - **The host NIC counter is not a WAN proxy.** Over a 3-minute window `enp3s0` moved
      **7.26 GB while the WAN moved ~2.6 MB — 99.96% of it was LAN** (Samba/Jellyfin serving the
      house). The `net.up`/`net.down` samples the dashdot source already stores must never be
      wired to a quota.
    - **The router's UPnP `WANCommonInterfaceConfig` counters are fabricated.** A 50 MB download
      moved them 1.59 MB; they tick at a steady ~7 KB/s regardless of real traffic. Its
      `RECORD_ENTRY` is equally wrong with `RECORD_CFG.allDevices=0` (753 KB during a 60 MB
      download) and correct with it set to 1 — same box, same API, one field apart.
    - **`STAT_ENTRY` (gated by `STAT_CFG.enable`) is the one that works.** A 60 MB download was
      counted as 63,238,079 B against 62,906,209 B on the host NIC — **0.5%**. Reachable from the
      container over the bridge, so no `CAP_NET_ADMIN`, no privileged sidecar, no host changes.
    **LAN really is excluded, wired and wireless alike** — this was measured, not assumed, because
    wired↔wireless traffic crosses the router's bridge and wired↔wired does not:
    a Jellyfin stream pushed **437,701,354 B** off the server's NIC in 180 s while the router
    recorded **167 KB** for the receiving TV (0.04%), and a 326.7 MiB episode downloaded to a
    phone over wifi left that phone's whole-day total at 8.93 MB of its own background internet.
    Six things bite when extending this:
    - **The router allows exactly ONE admin session.** `USER_CFG` is unsupported on this firmware
      and a second login *silently* invalidates the first — the evicted session then answers
      HTTP 500, which is why `RouterClient.cgi` re-authenticates once on a 500 and no more. It
      also means polling logs *you* out of the router's own web UI and vice versa. That is what
      the Bandwidth tab's pause control is for, and why it stores a **deadline, not a flag**: a
      forgotten pause would quietly stop recording the household's usage with nothing visibly
      stuck to remind you.
    - **The delta engine counts only the difference between two values it has itself observed.**
      Everything else is a baseline. That single rule replaced three that read plausibly and were
      wrong: a cold start counts nothing (right, and always was), but "a new MAC counts its
      current value — the router's counter for a device starts when it first sees it" and "a
      counter going backwards is a reset, so the current value is the new traffic" are both
      **false**, because the router *keeps* a device's counter while the device is missing from
      `STAT_ENTRY`. Combined with a cursor that was `DELETE`d the moment a device missed one poll,
      a flapping device had its whole accumulated counter re-counted every minute: **1,249 GB
      recorded for one device in one day on a link whose physical ceiling is ~326 GB/day**, while
      its cursor read 0.29 GB. So **cursors are retained across an absence and expire by age**
      (`wan_usage._CURSOR_TTL`, 7 days) — retention is what tells "the counter kept running"
      (subtract) from "the counter restarted" (baseline), and deleting it destroyed exactly that.
      The proof is unambiguous in the log: the identical `rx=3618567324` was offered and rejected
      **963 times in a row over 23 hours**. The counter never moved.
      A counter going backwards is still a reset and never a 32-bit wrap (a wrap needs 4.29 GB,
      which cannot happen inside one poll interval; `RouterSource` asserts that at startup rather
      than leaving it as a comment for someone to violate by raising `interval_s`) — it just
      re-bases and counts nothing now. Note a retained cursor spans gaps far longer than one poll,
      where that assertion says nothing; that is safe because a wrap across such a gap either
      reads as backwards (baseline) or under-counts by 4.29 GB, and both err downwards. **An
      under-count is a gap; an over-count is a fiction that looks like data.**
    - **One MAC can be two counters.** `STAT_ENTRY` sometimes lists a MAC twice in one poll:
      a frozen host-table entry the router never cleaned up (hostname `Unknown` or the bare
      MAC) beside the live, restarted one, and the pair persists for a day or more. With one
      cursor per MAC the two rows took turns overwriting it, so every poll differenced the
      stale row against the live value and counted the gap. The old code even carried a comment
      calling that "safe", because it had only been tested with an *identical* duplicate. logy
      recorded **427 GB over 13-14 Sep** with a stale `rx=199743377` beside a live counter that
      only moved 99 MB, and Redmi-Note-11 recorded 292 GB over 17-18 Sep. About 720 GB was
      fabricated, and it fired a false 100% quota alert. The plausibility bound caught only the
      devices whose stale row sat *above* the live one by more than one poll's allowance (the
      TV box, rejected 7,907 times). The same failure landed on either side of the bar, which
      is exactly the "generous bound" lesson below. Stored counters are now a *set* per MAC
      (`wan_counters`), and `wan_usage._match` pairs each row with the counter it continues
      (never backwards, smallest delta), so the frozen row always pairs with itself.
    - **The metrics cross-check now runs itself.** `worker._check_wan_reconcile` compares
      yesterday's `wan_usage` total against Σ `wan.rx`+`wan.tx` × `interval_s` once a day. It
      sends a `wan-reconcile` ntfy when the ratio is outside ±15% and names the heaviest device,
      which is where the bad row has been every time. Done by hand, the duplicate-row bug was
      found 12 days late, with `metrics`' 30-day retention already running down the evidence.
      Days with <90% sample coverage (router paused or unreachable) get no verdict, because the
      delta engine differences across the gap while `metrics` has a hole.
    - **A plausibility bound set generously is not set at all.** A delta larger than the link
      could physically have carried is dropped and re-baselined — the router emits exactly that
      after a settings change (**a measured 27 GB in one minute**). But `link_max_bytes_s` was
      left at 25 MB/s (200 Mbit) on a ~30 Mbit line, so the bar sat at 2.25 GB per 60s poll. That
      is the *only* reason the bug above was invisible: `logy`'s counter happened to sit at 3.6 GB
      and was caught 963 times, while the TV box's sat at ~1 GB and injected 1.2 TB in silence —
      same failure, same minute, one above the bar and one below. **Set it to the real line rate**
      (here 4 MB/s against a measured 3.78 MB/s ceiling). Too low is visible, because every
      rejection is logged; too high produces no rejection, no warning and no symptom.
    - **`metrics`' `wan.rx`/`wan.tx` are an independent check on the engine, and this is how to
      run it.** They are sampled from `currBytes`, a different field of the same `STAT_ENTRY` row,
      so they do not share the delta engine's failure modes. Sum them per day, calibrate the
      scale factor against days you have reason to trust (60.36 here — `currBytes` is a per-second
      rate over the router's own 60s window), and the ratio against `wan_usage` localises a bad
      row to a **single device-day**: it read 1.00 on every clean day and 2.13/1.38/1.54/6.03/40.2
      on the five days that carried a bad row. That is what identified all six poisoned rows, and
      it is the acceptance test for any repair — "the number looks smaller" is not.
      **`metrics` has 30-day retention and `wan_usage` has none**, so this instrument expires
      while the data it audits does not: run the check within a month of a suspect day.
    - **Settings writes land in the router's flash.** `ensure_stats_enabled` reads first and
      writes only on a difference, once per process. Never write on a schedule.
    - **Storage is keyed by MAC; identity is resolved at read time.** One device is not one MAC —
      the Xiaomi TV box reached the router under **three** locally-administered addresses and
      filled three rows, and `Redmi-Note-11` two. `fold_identities` groups on alias-or-hostname on
      the way out, so a rename or a new `device_aliases` entry re-groups history that is already
      stored instead of freezing whatever the device was called when the bytes were counted. Two
      traps: the router stores the **raw MAC as the hostname** while it is re-creating a host-table
      entry, so `by_device` must prefer a day that reported a real name or one device splits into a
      named row and a bare-MAC row; and the live "Now" column must join on identity too, or a
      phone that rotated its address mid-window reads `—` while it is actively downloading.
    - **Two datasets with different start dates cannot be divided.** The "Tracked as downloads"
      tile compares the ledger against household WAN. The ledger holds months; the WAN record
      begins *partway through* the day the source was switched on. Clamping the ledger to the WAN
      record's first **day** was not enough — it still read a confident "100% tracked" in
      production off a twenty-minute-old record, with the hover text admitting "369.49 MB of the
      household's 47.58 MB". The share is now computed only from the first day WAN data covers
      end to end, and until such a day exists the tile says when recording began instead of
      inventing a number. **A `min(x, 1.0)` clamp does not make a ratio meaningful — it hides
      that the denominator is the wrong shape.**
    Note the panels on the Bandwidth tab poll at 60s (matching `router.interval_s`), not the
    `data-interval="0"` the ledger panels beside them use: the By-device table has a live "Now"
    column, and at 0 it froze at page load — the same stale-as-live trap as pitfall 11.
    And note `SCAN_VERSION`-style versioning (pitfall 16) does **not** apply here: `wan_usage`
    holds observations, not a cached derivation of a formula, so there is nothing to re-derive —
    which is exactly why repairing a bad row is a `DELETE` and not a version bump.

24. **The >6-channel AC-3 compat path is the one branch of the classifier that is not
    idempotent, and nothing outside the file's own track list can tell you it already ran.**
    `convert` (3-6ch non-Dolby) **replaces** the source, so the output is `ac3` and the next run
    reads it as passthrough. `compat` (>6ch, where ffmpeg's encoders cap at 6) **keeps** the
    source and **adds** a 5.1 downmix beside it — so the 7.1 AAC is still a 7.1 AAC, and
    `classify_audio`, which sees one `Track` and nothing else, says "add a downmix" again. And
    again. Twin Peaks S1 was processed, Sonarr **renamed** the files, S2 was added, and the
    rescan re-flagged S1: 7 episodes ended up with two identical `[5.1 AC-3 compat]` tracks
    (measured: 20 files in the library carry a downmix at all, 7 carried two; the audit log holds
    two `prune` rows for the same episode 2 days apart under different names).
    - **The `(path, mtime)` audit guard could not cover this, and it has been deleted.**
      `steps.prune` used to skip a file whose last successful prune matched
      `already_done(path, "prune", mtime)`, but an arr rename changes the path, a restore
      changes the mtime, and the row is deleted at `worker.retention_days` (30). A guard that
      fails open on a rename is not a guard against a renamer. It failed a second way during
      the cleanup of this very bug: it sat *after* `decide()` in the step, so it overrode a
      live decision with a cached one, and all 20 re-runs returned `already pruned (same
      mtime)` without doing anything — a stale yes/no beats a correct policy every time.
      **`decide()` is the whole test**: it is computed from the file's current contents, so
      work it reports is work that is genuinely outstanding. `Audit.last_done_at` survives for
      *display* (`pruned_at`); `already_done` is gone so it cannot be reintroduced.
      Idempotency comes from the file itself, which is what `classify_audio_plan` does: a
      `compat` action is dropped when a **kept** AC-3/E-AC-3 track of ≤6 channels in the same
      language is already there. Scoping it to *kept* matters — dropping that track via
      `--audio` has to re-enable the encode, or the output carries nothing a limited player can
      decode.
    - **A skipped prune still calls `mark_clean`.** `_sync_library_row` returns early only on
      `FAIL`, so those 20 no-op skips stamped `needs_work=0` on rows that were not clean — and
      because `mark_clean` leaves `scan_version` alone, the next *ordinary* scan cache-hits on
      (path, mtime, size) and keeps the lie. Undoing it needs a `full` scan (or another
      `SCAN_VERSION` bump). Worth remembering whenever a step is made to skip work it used to do.
    - **Suppression is decided on codec, not on the `[5.1 AC-3 compat]` name.** An authored AC-3
      5.1 serves exactly the same purpose as one mkvprune would add, and `classify.py` is
      explicit that classification never reads a track title. The name *is* trusted in one
      place — `mux.is_compat_track`, used to spot **duplicates** and to move the default flag —
      because there it is mkvprune's own provenance stamp, not a claim about a codec.
    - **The downmix now takes the default flag** from the source it stands in for. The flag on a
      7.1 AAC points every limited player at the one track it cannot decode. Note the stand-in
      has two shapes and the second is the one that is easy to miss: the downmix being added by
      this mux, **and** one the file already carries. On a re-mux of a processed file there is no
      `compat` action at all (it was suppressed as redundant), so without the second case
      `resolve_default` keeps the surviving source default and the flag never moves.
    - **Cleanup runs through the policy, not a script.** `decide()` reports a surplus downmix as
      a `dropped_audio` and a misplaced default as `compat_default_fix`, both under the new
      `compat` reason tag, so the Library tab's ordinary scan → select → re-run flow repairs
      them with an audit row, an ntfy, and the per-item Jellyfin refresh (pitfall 18). Both are
      pure remuxes — zero encode jobs. `compat_default_misplaced` must mirror
      `mux._compat_stand_in` exactly: flag a file the remux would not actually change and it
      stays "needs work" forever.

25. **A container change re-derives ffprobe's `r_frame_rate`, so an exact compare of it makes
    `remux_to_mkv` unpassable.** mp4 states its rate in the movie timescale — ffprobe read
    *The Deer Hunter*'s as `13978/583` — while Matroska stores a nanosecond `default_duration`
    that mkvmerge snaps to the nearest well-known value (`41708333` ns = `24000/1001`). Same
    rate, two spellings, **1.6e-6 apart**, and `verify._check_video` compared the strings. The
    only mp4 import this library has ever had dead-lettered at exit 3 after a 6-minute mux that
    was provably correct: the output's own mkvmerge statistics tag reported
    `NUMBER_OF_FRAMES=264007` against the source's ffprobe `nb_frames=264007`, and the duration
    matched to 42 ms. `r_frame_rate` is now compared numerically within
    `_FRAME_RATE_TOLERANCE`, falling back to the exact compare when either side is unusable
    (ffprobe writes `0/0` when it cannot derive a rate).
    - **The tolerance is set by what must stay fatal, not by what was observed.** The NTSC
      pulldown pairs (24 vs 24000/1001, 30 vs 30000/1001, 60 vs 60000/1001) sit `1/1001` =
      **0.0999%** apart and are a real rate change; no genuine change is subtler. 1e-4 leaves
      10x margin under that and 10x over the widest drift measured here (`2997/100` vs
      `30000/1001`, 1.0e-5). Set generously it would stop distinguishing 24 from 23.976 —
      pitfall 23's "a bound set generously is not set at all".
    - **Loosening it costs nothing, because r_frame_rate was never the check that mattered.**
      Matroska carries explicit per-block timestamps; `default_duration` is a hint, not the
      playback clock. Real timing regressions are caught by `_check_duration` and the track
      composition count. This is the same shape as the `uid`/`default_duration` skip two lines
      below it — **a non-Matroska source simply has no Matroska answer to compare against**.

26. **Quotas are attributed from the router; the ledger only moves bytes between them, and it
    could never have measured them.** The Quota tab (`orchestrator/quota.py`, pure and
    DB-free like `fold_identities` and `library/estimate.py`) answers "whose" on top of the two
    stores that already answer "how much" and "which device": every figure starts as a device's
    WAN total, config maps device → person → quota, and a ledger row with a `quota` set moves
    *its own* bytes to another bucket. **Σ over quotas therefore always equals what the router
    measured**, which is rendered on the page and not merely asserted in a test, because the one
    failure this design cannot tolerate is a total that looks reasonable and is wrong.
    - **The old quota alert read `ledger.totals()`, and that is the bug the feature exists to
      fix.** Over one real period the ledger held 409.05 GB while the link carried 505.36 GB, so
      the household was told 81% of its 500 GB plan while already over it. The ledger is a
      partial record of one host's torrents; it is not a measurement of the link and never was.
      `worker._check_quota` now reads `services.attribution(...)`, the single seam the tab uses
      too — two answers to the same question is how `ntfy.status_granularity` came to gate two
      emitters and not the other six.
    - **Reassignment is period-level arithmetic, never day-level.** A ledger row is stamped with
      the day the download *completed* while its bytes crossed the link over however many days it
      ran. Nine `[BO3]` rows are stamped 8 Sep 2026 totalling 54.94 GB against PC-114's 31.86 GB
      of WAN *that day*; over 5-8 Sep the same device moved 62.20 GB and it reconciles. Carving
      per day would have driven a device negative and invented the difference somewhere else.
      A carve-out bigger than the source device's period total is clamped **and the shortfall
      reported** (`Attribution.dropped`) — absorbing it quietly would keep the totals adding up
      while telling the wrong story about a device.
    - **A row older than the WAN record moves nothing.** Recording began 29 Aug 2026 and the
      ledger reaches back to 23 Aug; eleven rows sit in that gap. Moving them anyway would carve
      bytes out of a device's *later* days to pay for a download nothing was watching. Same rail
      as the Bandwidth tab's "tracked as downloads" tile (pitfall 23).
    - **`downloads.device`/`.quota` are nullable and NULL means *derive it*, not *unknown*.** A
      NULL device resolves to `quotas.server_device` and a NULL quota to that device's own, so
      renaming the server or moving a person between quotas re-attributes history already stored
      — the same reason `fold_identities` resolves identity at read time. That is also why the
      one-shot backfill only writes the games rule (`kind='game'` → `quotas.games_device`,
      11 rows / 59.02 GB here) and leaves the other 27 rows alone. Ownership in
      `quotas.devices` is likewise keyed to the **folded identity**, never a MAC: the TV box
      already reaches this router under three.
    - **Attribution goes through `Ledger.set_attribution`, not `update`.** `update` marks a row
      `edited`, which permanently stops qBittorrent refreshing it. Attribution says *whose* bytes
      these were, not *how many* — freezing the byte count because someone corrected the
      bookkeeping is the wrong trade in the wrong direction.
    - **A quota with no allocation has `pct is None`, and None is not zero.** It is tracked but
      unbounded, so it shows share-of-household in a neutral hue and raises no alert. A green bar
      would imply a limit it is comfortably inside; an alert would page about a pot whose size
      nobody has decided.
    - **The Quota tab writes config, so it writes exactly one block.** `quotas.devices` is
      rewritten by `mediacore.config.replace_mapping_block`; dumping the validated model would
      resolve every `${VAR}` into the file and flatten every comment, which is what
      `config_schema.py` already refuses to let the UI do. Comments *inside* that one block are
      the whole cost. `QuotasConfig` sets `validate_assignment=True` so setting a field on an
      already-built config really rebuilds the models under it — the `status_granularity` trap
      from pitfall 22, closed at the model this time.
    - **A period's `end` is an exclusive instant, and `wan_usage` stores days.** Truncating the
      end to a date made it inclusive, so a boundary at midnight put that whole day in *both*
      windows — the current period asking `day >= '2026-09-11'` and its predecessor
      `day <= '2026-09-11'`. Each window still reconciled internally, which is why nothing
      flagged it: the day was simply counted twice, once on each side. `_range_where` now takes
      the day containing `end - 1s`, leaving a bare `YYYY-MM-DD` end alone because that names a
      day rather than an instant (`totals(start=day, end=day)` asks for exactly that day).
    - **A mid-day renewal is exact only if the start day was snapshot at the renewal instant.**
      `wan_usage` accumulates into one row per device per day, so *at that instant* the day's row
      holds exactly the traffic that predates the new period — and one poll later it does not.
      That is why `quota_period_splits` is **captured, never derived**, and why the UI's
      "Renew now" exists instead of a second time field: the snapshot and the start time have to
      describe the same moment, which a time typed hours later cannot. Moving a period's start
      discards its snapshot (`clear_split`) — a precise answer to the wrong question is worse
      than the honest fallback. An empty snapshot still counts as captured: "nothing had been
      used yet today" is a real answer. `Services.period_subtraction(period, side=...)` is what
      divides the day — `after` keeps the day minus the snapshot, `before` keeps only it — and
      a test asserts the two halves still sum to the whole.
    - **Without a snapshot, a quota period renewed mid-day surrenders its whole start day to the
      previous period.**
      `wan_usage` is one row per device per *day* and nothing in this database records per-device
      usage more finely — `metrics` is timestamped but household-aggregate and a calibrated
      estimate, so apportioning it per device would invent exactly the kind of number pitfall 23
      forbids. So a day belonging to two periods goes wholly to the older one
      (`QuotaPeriod.effective_start` is the next midnight), which under-counts the new period by
      whatever ran between the renewal and midnight. That is the direction to err: crediting the
      new period with the whole day hands it traffic that mostly predates it. `start_time` is
      not decorative under this rule — it is what decides the start day belongs to the previous
      period at all. Use `effective_start` everywhere a period defines a window; `starts_at` is
      the renewal moment and is for display only.
    Note `cutoffs` was renamed to `quota_periods` (`cutoff_date` → `start_date`) by a guarded
    `ALTER TABLE` in `QuotaPeriods.__init__`. **It has to run before the schema script**: a
    `CREATE TABLE IF NOT EXISTS quota_periods` first would make an empty table, the guard would
    see the destination present and skip, and every existing period would sit orphaned in
    `cutoffs` while the UI showed none — data loss wearing the costume of a clean install.


## Origin note

mkvprune and seasonal-shift were **copied** (not `git subtree`-imported) from
`/mnt/vault/data/projects/{mkvprune,seasonal-shift}`; the copies here are the source of truth. Git
history of the originals was not preserved. seasonal-shift's reachable core has since been
absorbed into `mediacore.renumber`; the standalone CLI, watcher, doctor, undo and Sonarr client
are gone (see Layout).
