# Operations (home-server deployment)

project-pilot runs as a long-lived Docker container next to its own PostgreSQL,
on Nik's home server. No ingress or domain is needed.

## Prerequisites

- Docker with Compose v2.
- A filled-in `profile/profile.md` and `profile/constraints.yaml` (baked into the
  image at build time; editing the profile means rebuild + restart).
- A `.env` file (copy `.env.example`) with the real values: `CONTACT_MAIL`,
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `OPENAI_API_KEY`, `LLM_MODEL`,
  `SEARCH_URLS` (sorted "newest first"), and optionally `POSTGRES_PASSWORD`. Do not
  set `DATABASE_URL` in `.env`; compose sets it to reach the postgres service.

## Build and run

```sh
docker compose build          # build the app image (needs Docker Hub + ghcr access)
docker compose up -d          # start postgres + app
docker compose logs -f app    # watch the worker
```

On start the app container runs `init-db` (Alembic `upgrade head`) via its
entrypoint, then `daemon`. The daemon performs one scan immediately (so the
healthcheck has a baseline) and then every `SCAN_INTERVAL_MIN` minutes.

## Images and services

- **app**: multi-stage build on `python:3.13-slim`, dependencies installed with
  `uv`, runs as the non-root user `pilot`. Entrypoint applies migrations then execs
  the CLI (default `daemon`).
- **postgres**: `postgres:16` with a named volume `pgdata` for persistence and a
  `pg_isready` healthcheck. The app waits for postgres to be healthy.

## Healthcheck

The app healthcheck runs `project-pilot healthcheck`, which exits 0 only if the
last successful (or partial) run finished within `3 x SCAN_INTERVAL_MIN` minutes.
This covers both process liveness (the command runs) and freshness (a recent
successful scan). `start_period` gives the first scan time to complete.

## Common commands (inside the container)

```sh
docker compose exec app project-pilot stats         # reporting summary
docker compose exec app project-pilot run-once      # one scan now (exit != 0 on failure)
docker compose exec app project-pilot test-notify   # send a Telegram test message
docker compose exec app project-pilot healthcheck   # liveness/freshness probe
```

## Threshold tuning

After a few days, run `stats` and look at the verdict distribution and the stored
scores. Raise `MATCH_THRESHOLD` if you get too many weak alerts, lower it if real
matches are being missed. Restart the app after changing `.env`.

## Troubleshooting

- **Container is unhealthy**: no successful run in `3 x` the interval. Check
  `docker compose logs app` for a `SourceBlockedError` (cooldown) or config error.
- **Cooldown**: a 403 or captcha sets a 6-hour cooldown in `source_state`; the
  worker skips scans until it expires and sends one Telegram warning. If it
  persists, the site may be blocking automated access (revisit `docs/compliance.md`).
- **Selector breakage** (`SelectorMismatchError`): freelancermap changed its
  markup. Update the selector constants at the top of
  `src/project_pilot/ingestion/parser.py`, refresh the fixtures, and rebuild.
- **Repeated failures**: three consecutive failed runs send one Telegram warning.
- **Profile changes**: edit `profile/`, then `docker compose build && docker
  compose up -d`. (Alternatively, mount `./profile:/app/profile:ro` in
  `compose.yaml` to edit without rebuilding, then restart.)

## Migrations

Schema changes ship as checked-in Alembic migrations and are applied automatically
on container start. To apply manually: `docker compose exec app project-pilot
init-db`.
