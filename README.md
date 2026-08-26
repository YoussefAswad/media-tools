# media-tools

A uv-workspace monorepo consolidating three media tools behind one shared core and an
ARR-driven orchestrator. Each tool remains usable on its own; the orchestrator composes them.

```
packages/
  mediacore/       shared: ARR client, config (+${ENV} expansion), ntfy, audit, path-map, probe facade
  mkvprune/        prune MKV tracks + normalize audio to AC-3, preserving video/DV/HDR (pure, no-network)
  seasonal-shift/  renumber SxxExx episode files by per-show offsets, with Sonarr sync
  quality-check/   probe-based audio/subtitle quality rules + the prune decision
  orchestrator/    FastAPI webhook service + durable SQLite queue/worker + web UI
deploy/            Dockerfile, compose fragment, example config (see deploy/README.md)
```

## What the orchestrator does

Radarr/Sonarr fire a **Webhook** on import → the orchestrator queues a job → a single worker runs
the pipeline for that file:

1. **seasonal-shift** — renumber if the show has configured offsets (Sonarr manual-import).
2. **quality-check** — probe and ntfy any findings (non-Dolby/DTS multichannel audio, foreign
   image subs, too many audio tracks).
3. **prune** — keep every audio track (mkvprune re-encodes only non-Dolby/DTS multichannel to
   AC-3) and drop non-eng/ara **image** subtitles; then rescan the arr.
4. **status** — post an ntfy rollup of what happened.

Every action is written to a JSONL + SQLite **audit log** and shown in the web UI (active/failed
jobs, recent actions, per-job trail, config editor with validation + hot-reload).

## Develop

```bash
uv sync
uv run pytest              # mediacore + quality-check + orchestrator
uv run ruff check .
```

External tools required for real runs: `mkvmerge`, `mkvpropedit`, `ffmpeg`, `ffprobe`.

## Deploy

See [deploy/README.md](deploy/README.md).
