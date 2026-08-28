# Deploying the media orchestrator

The orchestrator receives Radarr/Sonarr **Webhook** events and, per file: records the download's
bandwidth, flags quality issues (→ ntfy), prunes unwanted subtitle/audio tracks and re-encodes
non-Dolby/DTS multichannel audio (mkvprune), renumbers the episode if the show has offsets,
rescans the arr, refreshes the item in Jellyfin, and posts an ntfy status. Everything is recorded
in an audit log and shown in a web UI, which also carries a Library tab for the back catalogue.

## 1. Build the image

```bash
cd media-orchestrator
docker build -f deploy/orchestrator/Dockerfile -t youssefaswad/media-orchestrator:latest .
```

The image bundles `mkvtoolnix` + `ffmpeg` and the whole workspace (mkvprune, mediacore,
orchestrator).

## 2. Configure

Copy `deploy/config.example.yaml` to the mounted config dir as `config.yaml` and set the
referenced env vars (secrets stay as `${VAR}` references in the file). The library **must** be
mounted at the same path Radarr/Sonarr use, or set `arr.*.media_root` / `local_media_root`.

## 3. Add the compose service

Merge `deploy/compose.orchestrator.yaml` into `my-media-server/docker-compose.yml` (fix the
build context, the `/data` mount, and the env). Then:

```bash
docker compose up -d media-orchestrator
```

## 4. Point Radarr & Sonarr at it (Webhook Connect)

Settings → Connect → **+ → Webhook**:

- **URL:** `http://media-orchestrator:8000/arr/radarr` (Sonarr: `.../arr/sonarr`)
- **Method:** POST
- **Triggers:** On Import, On Upgrade, On Rename
- **Header:** `X-Webhook-Token: <ORCH_WEBHOOK_SECRET>`

A **Test** returns `{"status":"ok","test":true}` without side effects.

The existing `scripts/arr-quality-check.sh` Custom Script can stay as a fallback during cutover,
then be removed — its rules are reimplemented in `mediacore.policy`.

## 5. Expose the web UI via Caddy

Add to `my-media-server/caddy/Caddyfile` (adjust host), then recreate Caddy (this project needs a
force-recreate, not a reload):

```
orchestrator.example.com {
    basicauth {
        # caddy hash-password --plaintext 'yourpass'
        admin <bcrypt-hash>
    }
    reverse_proxy media-orchestrator:8000
}
```

```bash
docker compose up -d --force-recreate caddy
```

## Monitoring tab

The web UI's **Monitoring** tab watches disk usage, host system metrics, container up/down, and
drive SMART, with threshold alerting to ntfy + an in-app view. Everything is configured under the
`monitoring:` block in `config.yaml` (see `config.example.yaml` for the full annotated schema) and
is **off by default per source** except disk usage. Each source is a best-effort client — if it's
unreachable the tab shows a hint and the worker keeps running.

| Source | Needs | Notes |
|---|---|---|
| **Disks** | nothing (uses paths already mounted) | watch any path the container can `stat`; % or absolute-free thresholds. To watch the host root, bind-mount it (e.g. `- /etc:/hostroot:ro`) and point a watch at it. |
| **Containers** | `- /var/run/docker.sock:/var/run/docker.sock:ro` + `group_add: ["<docker gid>"]` | up/down + per-container CPU/mem. `docker.watch` limits *alerting* (the list always shows all). |
| **System** (CPU/RAM/temps/net) | a reachable **dashdot** container | reads dashdot's REST API — no host access granted to the orchestrator. **dashdot must run `network_mode: host`** for the network widget to read the real NIC (on the bridge it only sees its own interface); then reach it via `host.docker.internal` (`extra_hosts: ["host.docker.internal:host-gateway"]`). |
| **Drives (SMART)** | a **Scrutiny** sidecar (`SYS_RAWIO`/`SYS_ADMIN` + disk devices) | reads Scrutiny's API — the collector holds the raw device access. USB-bridge drives often don't expose SMART. |

Alert rules debounce (`sustain_s`), apply recovery hysteresis (`clear_s`), de-dupe, and throttle
reminders (`cooldown_s`); **thresholds are host-specific — calibrate against real readings** (e.g.
a Ryzen Tctl sensor idles hot, so raise the CPU-temp thresholds). Editing `config.yaml` on disk is
not hot-reloaded — save via the Config tab, or restart the container.

## Standalone tool

`mkvprune` is the one component that still runs on its own, and the image ships it on `PATH`:

```bash
mkvprune movie.mkv                 # interactive prune/normalize
```

The quality rules and the episode renumberer are library code (`mediacore.policy`,
`mediacore.renumber`), not CLIs — they were absorbed once `decide()` came to depend on arr
metadata that a standalone command can't supply.
