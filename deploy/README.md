# Deploy

Two files. `Dockerfile` builds the gateway from the repository root into `python:3.12-slim`,
runs as a non-root user, installs with `pip install --no-cache-dir .`, and binds `0.0.0.0`
because loopback inside a container is unreachable from the host — safe only with the
exposure rules below. `docker-compose.yml` is one service: configuration from the repository's
`.env`, restart `unless-stopped`, read-only root filesystem, port on host loopback only.

## Start

```bash
cp .env.example .env        # then set EROUTER_ADMIN_TOKEN; run from the repository root
docker compose -f deploy/docker-compose.yml up -d --build
curl http://127.0.0.1:8450/health   # public, liveness only; dashboard is at /
```

## Where the data lives

The SQLite database, its WAL sidecars and rotating logs go to `EROUTER_DATA_DIR`, set by the
compose file to `/data` and mounted from `deploy/data/`. That database holds every upstream
vendor token in plaintext (`connections.cred_json`) plus request traces while observability is
on. Treat the directory as a secret: permissions, deliberate backups, never inside an image.

## Environment variables needed

| var | note |
|---|---|
| `EROUTER_ADMIN_TOKEN` | **required** — empty means every `/api/*` route answers 503 (fail-closed) |
| `EROUTER_REQUIRE_CLIENT_KEY` | `true` by default; keep it on once anything else can reach the port |
| `EROUTER_DATA_DIR` | set to `/data` by the compose file |
| `EROUTER_HOST` / `EROUTER_PORT` | `0.0.0.0` / `8450` in the image |
| `EROUTER_PROVIDERS_PATH` | only if a private adapter distribution is mounted in |

Full list with real defaults: `.env.example`.

## Exposing it

A reverse proxy that terminates TLS and authenticates, or an SSH tunnel / VPN. Never publish
`8450` on a public interface: this gateway holds tokens and spends money.
