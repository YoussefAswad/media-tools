# mediacore

Everything the orchestrator shares, in one library.

- `config`, `models` — Pydantic config with `${ENV}` expansion; the shared config sections.
- `audit`, `paths`, `probe` — JSONL+SQLite audit log, local↔remote path mapping, and a probe
  facade over mkvprune's reconciled track model.
- `arr/` — Radarr/Sonarr REST clients, webhook event parsing, and the arr-language → ISO table.
- `policy/` — probe-based quality rules and the mkvprune prune decision (was `quality-check`).
- `renumber/` — `SxxExx` planner/executor for seasonal ⇄ absolute shifts (was `seasonal-shift`).
- `notify`, `qbit`, `jellyfin`, `dashdot`, `docker_status`, `scrutiny` — best-effort outbound
  clients. **They never raise**: on any failure they log and return `None`/`False`.
