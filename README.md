# media-orchestrator

A uv-workspace monorepo for an ARR-driven media orchestrator, plus the standalone MKV tool it
drives.

```
packages/
  mkvprune/        prune MKV tracks + normalize audio to AC-3, preserving video/DV/HDR
                   (pure stdlib, no network — invoked as a subprocess, never imported)
  mediacore/       everything the service shares: config (+${ENV} expansion), models, audit,
                   path-map, probe facade, ARR/ntfy/qbit/Jellyfin/monitoring clients,
                   policy/ (quality rules + prune decision), renumber/ (SxxExx shifts)
  orchestrator/    FastAPI webhook service + durable SQLite queue/worker + library index + web UI
deploy/            Dockerfile, compose fragment, example config (see deploy/README.md)
```

## What the orchestrator does

Radarr/Sonarr fire a **Webhook** on import → the orchestrator queues a job → a single worker runs
the pipeline for that file:

1. **ledger** — snapshot the download's bandwidth from qBittorrent (first, so it survives a
   later failure).
2. **quality-check** — probe and ntfy any findings (non-Dolby/DTS multichannel audio, foreign
   subs, too many audio tracks).
3. **prune** — mkvprune drops unwanted subtitle/audio tracks and re-encodes non-Dolby/DTS
   multichannel audio to AC-3; then rescan the arr.
4. **seasonal-shift** — renumber if the show has configured offsets (Sonarr manual-import).
5. **Jellyfin refresh** — re-probe the changed item so its track list and size are not stale.
6. **status** — post an ntfy rollup of what happened.

A **Library tab** covers the back catalogue: a scan job walks the configured roots, records what
the pipeline *would* do to each file (probe + a pure `decide()` — nothing is rewritten), and
queueing any subset of it is a separate, explicit click.

Every action is written to a JSONL + SQLite **audit log** and shown in the web UI (active/failed
jobs, recent actions, per-job trail, config editor with validation + hot-reload).

## Develop

```bash
uv sync
uv run pytest              # the whole workspace in one pass
uv run ruff check packages
```

External tools required for real runs: `mkvmerge`, `mkvpropedit`, `ffmpeg`, `ffprobe`.

## Deploy

See [deploy/README.md](deploy/README.md).
