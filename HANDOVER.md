# project-pilot handover

> **Historical snapshot (MVP handover).** The system has since moved on:
> notification is now **Slack**, not Telegram (see `blueprint/build-plan.md`
> features 16–18 and `docs/slack-app-setup.md`); the Telegram setup steps below no
> longer apply. Current setup and operations: `README.md` and `docs/`.

Built end to end in one session with the ai-blueprint workflow: a Python 3.13
async worker that watches freelancermap.de, persists every listing losslessly in
PostgreSQL, evaluates fresh ones (hard rules then an LLM match), and alerts real
matches via Telegram. Strictly typed (`mypy --strict`), lint- and format-clean
(`ruff`), and covered by 132 tests.

**Quality gate (all green):** `uv run ruff check` and `uv run ruff format --check`
and `uv run mypy` and `uv run pytest` (132 passed, 91% coverage; core logic
modules at or above 90%).

## Built

All 12 MVP features are done. No feature is blocked; the two items that could not
be executed inside the build sandbox are infrastructure limits, not code gaps, and
are recorded as NIK-TODOs below.

| # | Feature | Status |
|---|---|---|
| 1 | Compliance check & source verification | done (live snapshot deferred, see below) |
| 2 | Config & profile foundation | done |
| 3 | Data model & migrations | done |
| 4 | Scraper ingestion (client, parser, normalize, watermark) | done |
| 5 | Freshness gate & hard rules | done |
| 6 | LLM matching (structured output, retry, fallback) | done |
| 7 | Telegram notification | done |
| 8 | Scheduler & pipeline runner | done |
| 9 | Resilience & self-monitoring (retry, cooldown, warnings) | done |
| 10 | Reporting basis (`stats`) | done |
| 11 | Docker & home-server deployment | done (image build deferred, see below) |
| 12 | README & legal | done |

Post-MVP items 13 to 15 (Telegram commands, re-evaluation, queue-based multi-source
workers) are intentionally left unchecked in `blueprint/build-plan.md`.

Each feature has an archived spec in `blueprint/history/features/`.

## Compliance finding (Feature 1)

**Go / no-go: not yet decided; deferred to Nik's first live run.** The build
sandbox could not reach freelancermap.de: the organizational egress policy blocks
the host (the proxy answered 403 to CONNECT), and its rules forbid routing around
a policy denial. This is an infrastructure limit, not one of the project STOP
conditions (which require actually reading robots.txt, the ToS, or a captcha).

Because the site was unreachable:

- **robots.txt / ToS:** not snapshotted. The four STOP conditions could not be
  evaluated and remain live checks for Nik before the first run.
- **Initial HTML vs JS:** unverified. Assumed server-rendered (freelancermap has
  historically been), to be confirmed with View Source.
- **`posted_at` granularity:** unverified. The code tolerates all cases via a
  `posted_at_precision` of `minute | day | unknown`.

**Chosen freshness strategy (robust to the unknown granularity):** prefer a
minute-precise `posted_at` (fresh if `now - posted_at <= ANALYSIS_WINDOW_MIN`);
otherwise use the gap rule against the watermark (fresh only if the last
successful run is within the window, so a backlog picked up after downtime is
persisted but not alerted). The pipeline is therefore correct whatever the live
granularity turns out to be.

Compliance is enforced in code at runtime regardless: a startup robots.txt gate
(including Crawl-delay) aborts on a disallowed path, the identifying user agent and
2 to 5 second delays are always applied, and a 403 or captcha triggers a 6-hour
cooldown instead of retrying. Full record: `docs/compliance.md` and
`docs/adr/0001-source-verification.md`. Parser development used clearly-labeled
synthetic fixtures (`tests/fixtures/`).

## NIK-TODOs

Before the first live run:

1. **Fill the profile.** Edit `profile/profile.md` (free-text profile) and
   `profile/constraints.yaml` (blacklist / must-have). These drive the matcher and
   the hard rules.
2. **Verify the source live** (on a networked machine). Read `robots.txt` and the
   Terms of Service; confirm the board paths are allowed and automated access is
   not forbidden (STOP if either fails). Save real list/detail pages over the
   synthetic fixtures in `tests/fixtures/` (keep the file names) and confirm the
   initial-HTML-vs-JS question and the `posted_at` granularity; adjust the selector
   constants at the top of `src/project_pilot/ingestion/parser.py` if needed, then
   re-run `uv run pytest`.
3. **Set search URLs.** Assemble 1 to 3 board search URLs (sorted "newest first")
   and put them in `SEARCH_URLS` in `.env`.
4. **Telegram.** Create the bot via @BotFather, set `TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`, then run `uv run project-pilot test-notify` (this live send
   was implemented and mocked in tests but not executed here).
5. **LLM.** Set `OPENAI_API_KEY` and `LLM_MODEL` (a small model is enough).
6. **First run.** `uv run project-pilot run-once`, watch the output, then start the
   `daemon` (or the Docker Compose stack).
7. **Build the image** on a machine where Docker Hub and ghcr are reachable
   (`docker compose build`). The build could not run in the sandbox (the registry
   CDN is blocked); the Dockerfile parses and both compose files validate with
   `docker compose config`.
8. **Tune** `MATCH_THRESHOLD` after a few days using `uv run project-pilot stats`.

No secrets were invented and no `.env` was committed; `.env.example` is the
template.

## Start commands

Local (with a PostgreSQL 16 on `localhost:5432`, e.g. via
`docker compose -f compose.dev.yaml up -d`):

```sh
uv sync                              # install dependencies
cp .env.example .env                 # then fill in the values
uv run project-pilot init-db         # apply migrations
uv run project-pilot run-once        # one scan (non-zero exit on a failed run)
uv run project-pilot daemon          # scheduler until SIGTERM
uv run project-pilot test-notify     # Telegram test message
uv run project-pilot stats           # reporting summary
uv run project-pilot healthcheck     # liveness/freshness probe
```

Docker (home server):

```sh
docker compose build
docker compose up -d
docker compose logs -f app
```

The app container applies migrations on start and then runs the daemon (with one
immediate scan). See `docs/operations.md` for the full operations guide and
`README.md` for setup and the compliance posture.
