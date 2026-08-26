# Deploying the media-tools orchestrator

The orchestrator receives Radarr/Sonarr **Webhook** events and, per file: renumbers it
(seasonal-shift), flags quality issues (quality-check → ntfy), prunes foreign image subtitles +
re-encodes non-Dolby/DTS multichannel audio (mkvprune), rescans the arr, and posts an ntfy status.
Everything is recorded in an audit log and shown in a small web UI.

## 1. Build the image

```bash
cd media-tools
docker build -f deploy/orchestrator/Dockerfile -t youssefaswad/media-orchestrator:latest .
```

The image bundles `mkvtoolnix` + `ffmpeg` and the whole workspace (mkvprune, seasonal-shift,
quality-check, mediacore, orchestrator).

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
then be removed — its rules are reimplemented in the `quality-check` package.

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

## Standalone tools

Each tool still runs on its own (outside the orchestrator):

```bash
mkvprune movie.mkv                 # interactive prune/normalize
quality-check movie.mkv            # print findings + prune decision
seasonal-shift run --config ...    # renumber episodes
```
