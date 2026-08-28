# orchestrator

The product: a FastAPI webhook service, a durable SQLite job queue + worker, a per-file dispatch
pipeline, a back-catalogue library index, a bandwidth ledger, host/container/SMART monitoring
with a debouncing alert engine, ntfy status, and a server-rendered web UI.

Composes `mkvprune` (as a subprocess) and `mediacore` (as a library).
