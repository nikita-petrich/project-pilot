# Operations (server deployment)

project-pilot runs as two long-lived Docker containers (the worker and the MCP
server) next to its own PostgreSQL. Only the MCP server needs a domain, and it is
published by the reverse proxy rather than by a host port — see
[`claude-setup.md`](claude-setup.md).

How the container gets onto the server is [`deployment.md`](deployment.md) (GitHub
Actions builds, the server pulls). This page is what to do once it runs.

## Prerequisites

- Docker with Compose v2.
- A filled-in `profile/profile.md` and `profile/constraints.yaml`. Both are versioned
  and baked into the image, so editing them means commit + deploy (or, when building
  on the host, rebuild + restart).
- A `.env` file (copy `.env.example`) with the real values: `CONTACT_MAIL`,
  `OPENAI_API_KEY`, `LLM_MODEL`, `SEARCH_URLS` (sorted "newest first"),
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `MCP_TOKEN`, and optionally
  `POSTGRES_PASSWORD`. Do not set `DATABASE_URL` in `.env`; compose sets it to reach
  the postgres service.

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
- **mcp**: the same image with `command: ["mcp"]`, serving the tools over
  Streamable HTTP behind its bearer token. No host port; the reverse proxy reaches
  it as `project-pilot-mcp:8765` on the `edge` network.
- **postgres**: `postgres:16` with a named volume `pgdata` for persistence and a
  `pg_isready` healthcheck. Both app containers wait for postgres to be healthy.

## Healthcheck

The app healthcheck runs `project-pilot healthcheck`, which exits 0 only if the
last successful (or partial) run finished within `3 x SCAN_INTERVAL_MIN` minutes.
This covers both process liveness (the command runs) and freshness (a recent
successful scan). `start_period` gives the first scan time to complete.

## LLM health alerts

A broken LLM is the one failure the healthcheck cannot see: scraping keeps working,
listings keep being stored, every run is recorded as a success — and every listing is
scored with the `llm_error` fallback, so no match alert can fire. The worker therefore
reports stage 3's health over the same channel as a match — a Claude session titled
as an operator warning:

- **On start**, the daemon makes one minimal call to `LLM_MODEL`. A model name that
  does not exist, a rejected `OPENAI_API_KEY` or an account out of credit is announced
  within seconds of the deploy, not at the next fresh listing.
- **After every run** that reached the LLM, a failure is announced with the cause
  (which setting, which account) plus the provider's own error message.
- **Repeats are throttled**: the same problem is re-announced only every 6 hours, a
  *different* cause alerts immediately, and recovery is announced once.

Hopeless failures (wrong model, rejected key, empty account) are not retried — the
identical call would fail identically, the same rule the scraper applies to a 403.

## Common commands (inside the container)

```sh
docker compose exec app project-pilot stats         # reporting summary
docker compose exec app project-pilot run-once      # one scan now (exit != 0 on failure)
docker compose exec app project-pilot test-match    # rules + LLM + a real push
docker compose exec app project-pilot healthcheck   # liveness/freshness probe
docker compose logs -f mcp                          # the MCP server's requests
```

## Threshold tuning

After a few days, run `stats` and look at the verdict distribution and the stored
scores. Raise `MATCH_THRESHOLD` if you get too many weak alerts, lower it if real
matches are being missed. Restart the app after changing `.env`.

## Troubleshooting

- **Container is unhealthy**: no successful run in `3 x` the interval. Check
  `docker compose logs app` for a `SourceBlockedError` (cooldown) or config error.
- **Cooldown**: a 403 or captcha sets a 6-hour cooldown in `source_state`; the
  worker skips scans until it expires and opens one warning session. If it
  persists, the site may be blocking automated access (revisit `docs/compliance.md`).
- **Selector breakage** (`SelectorMismatchError`): freelancermap changed its
  markup. Update the selector constants at the top of
  `src/project_pilot/ingestion/parser.py`, refresh the fixtures, and rebuild.
- **Repeated failures**: three consecutive failed runs open one warning session.
- **No sessions although listings keep arriving**: look for an LLM health warning
  session (see above), then check the stored cause:
  `select reason->'reasons' from evaluations where stage='llm' order by created_at desc limit 3;`
  Fix `LLM_MODEL` / `OPENAI_API_KEY` in the `prod` GitHub environment and redeploy —
  the server's `.env` is rewritten from GitHub on every deploy, so editing it on the
  server does not survive.
- **Profile changes**: edit `profile/profile.md`, then commit and push — the deploy
  rebuilds the image. When building on the host instead:
  `docker compose build && docker compose up -d`.
- **CV changes**: replace the PDF of the same name in the public Google Drive folder
  (`CV_DRIVE_FOLDER_ID`) — no commit, no redeploy. The app fetches each CV by name
  before every draft and send; see `docs/deployment.md`.

## Migrations

Schema changes ship as checked-in Alembic migrations and are applied automatically
on container start. To apply manually: `docker compose exec app project-pilot
init-db`.
