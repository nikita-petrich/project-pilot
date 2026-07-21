# project-pilot

A personal, single-user worker that watches freelancermap.de for new project
listings, persists every listing losslessly in PostgreSQL, evaluates fresh ones
against a profile (deterministic hard rules, then an LLM match), and reports real
matches to Telegram within minutes. Backend only, no web UI.

Built as a modern, strictly-typed Python codebase (Python 3.13, asyncio,
Pydantic v2, SQLAlchemy 2.0, `mypy --strict`). The binding detail specification is
[`SPEC.md`](SPEC.md); the design is summarized in
[`blueprint/context/project-overview.md`](blueprint/context/project-overview.md).

## How it works

Every `SCAN_INTERVAL_MIN` minutes (default 15) the worker:

1. Fetches the configured search URLs politely (identifying user agent, robots.txt
   gate, delays), paginating with a watermark so nothing is missed after downtime.
2. Persists every newly seen listing (lossless). On an empty database the first
   run seeds the full inventory without analysing or notifying.
3. For each new, fresh listing (within the analysis window), runs the evaluation
   pipeline: freshness gate, then hard rules from `constraints.yaml` (0 tokens),
   then an LLM match against `profile.md` producing a structured verdict.
4. Sends a Telegram alert for matches at or above `MATCH_THRESHOLD`, and stores a
   reason for every verdict (match and no-match alike) for later reporting.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 (locally via `compose.dev.yaml`, or your own instance)
- A Telegram bot (via @BotFather) and an OpenAI API key for live operation
- Docker with Compose for the containerized home-server deployment

## Setup

```sh
uv sync                              # install dependencies (creates .venv)
cp .env.example .env                 # then fill in the values (see below)
docker compose -f compose.dev.yaml up -d   # local Postgres on :5432
uv run project-pilot init-db         # apply migrations
```

Fill in the two profile files (they feed the matcher and the hard rules):

- `profile/profile.md` free-text profile (skills, desired projects, no-gos)
- `profile/constraints.yaml` hard rules (blacklist terms, optional must-have)

Set the environment values in `.env` (never commit real secrets; `.env` is
gitignored and `.env.example` is the template):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `CONTACT_MAIL` | inserted into the scraper user agent |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | your bot and your chat id |
| `OPENAI_API_KEY` / `LLM_MODEL` | LLM matching (a small model is enough) |
| `SEARCH_URLS` | comma-separated board search URLs, sorted "newest first" |
| `SCAN_INTERVAL_MIN` | default 15, validated to be >= 15 |
| `ANALYSIS_WINDOW_MIN` | default 30 |
| `MATCH_THRESHOLD` | default 60 |
| `LOG_LEVEL` | default `info` |

## Commands

```sh
uv run project-pilot init-db        # apply Alembic migrations
uv run project-pilot run-once       # one scan now (non-zero exit on a failed run)
uv run project-pilot daemon         # run the scheduler until SIGTERM
uv run project-pilot test-notify    # send a Telegram test message
uv run project-pilot stats          # reporting summary
uv run project-pilot healthcheck    # liveness/freshness probe (exit code)
```

## Running on the home server (Docker)

```sh
docker compose build
docker compose up -d
docker compose logs -f app
```

The app container applies migrations on start, then runs the daemon (with one
immediate scan). Full operations guide, including the healthcheck and
troubleshooting, is in [`docs/operations.md`](docs/operations.md).

## Threshold tuning

After a few days, run `stats` and inspect the verdict distribution and stored
scores. Raise `MATCH_THRESHOLD` if you get too many weak alerts; lower it if real
matches are missed. Restart the worker after changing `.env`.

## Troubleshooting

- **Cooldown**: a 403 or captcha sets a 6-hour cooldown in `source_state`; the
  worker skips scans until it expires and sends one Telegram warning.
- **`SelectorMismatchError`**: freelancermap changed its markup. Update the
  selector constants at the top of `src/project_pilot/ingestion/parser.py`,
  refresh the fixtures, and re-run.
- **Repeated failures**: three consecutive failed runs send one Telegram warning.
- **Container unhealthy**: no successful run within three times the interval;
  check `docker compose logs app`.

## Development

```sh
uv run ruff check           # lint
uv run ruff format --check  # format
uv run mypy                 # strict type check
uv run pytest               # tests (Postgres-backed tests skip if no DB)
```

Tests never make live network requests; freelancermap pages and external APIs are
served from fixtures or mocked. Test files live next to the code under `tests/`.

## Compliance and legal

project-pilot is for **personal use only**. It reads only pages that are publicly
reachable without login, with a clear, identifying user agent
(`project-pilot/1.0 (personal project alert bot; contact: <CONTACT_MAIL>)`), a
startup `robots.txt` gate (including `Crawl-delay`), a 2 to 5 second delay between
requests, and at most two list pages per search URL (detail pages only for new
listings). There is no login bypass, no captcha or bot-protection circumvention,
and no user-agent or proxy rotation. A 403 or captcha triggers a 6-hour cooldown
rather than retry hammering.

Before the first live run, confirm freelancermap's current `robots.txt` and Terms
of Service permit this use. The full compliance record, the STOP conditions, and
the first-run verification checklist are in
[`docs/compliance.md`](docs/compliance.md) and
[`docs/adr/0001-source-verification.md`](docs/adr/0001-source-verification.md). Do
not resell or redistribute scraped data.

## Project layout

```
src/project_pilot/
  config.py profile_loader.py errors.py db.py models.py repository.py
  ingestion/    client, parser, normalize, watermark
  evaluation/   freshness, rules, llm, schemas, prompts/
  notification/ telegram
  pipeline.py scheduler.py reporting.py cli.py
alembic/        async migrations
docs/           compliance.md, operations.md, adr/
tests/          unit + integration, fixtures/
```

Built with the [AI Coding Blueprint](blueprint/README.md); agent instructions live
in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
