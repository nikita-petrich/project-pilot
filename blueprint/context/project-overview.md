# project-pilot - Project Overview

> A personal, single-user worker that watches freelancermap.de, persists every
> listing losslessly, evaluates fresh ones against Nik's profile (hard rules then
> LLM), and pushes real matches to Telegram within minutes. Backend only.
> Binding detail spec: `SPEC.md` at the repo root.

## Problem

New freelancermap listings have to be judged within minutes or the application is
too late. Manual watching does not scale, and the platform's official
"Projektagent" only mails once a day. project-pilot polls the configured search
URLs every 15 minutes, evaluates each new listing automatically, and alerts only
on real matches while keeping a lossless record of everything for later
reporting. Secondary goal: serve as a verifiable, idiomatic modern-Python
reference for Nik's AI-engineer positioning, so code quality is itself a product
property.

## Users

Exactly one user: Nik (freelance full-stack and AI engineer). No multi-tenant, no
registration, no public product. Nik configures the profile and search URLs,
receives Telegram alerts, and tunes the match threshold over time.

## Features

Build-plan order (MVP 1-12). The headline is the evaluation-plus-alert loop
(features 5-8).

1. **Compliance check and source verification** - snapshot robots.txt and ToS, prove go/no-go, determine initial-HTML vs JS rendering and `posted_at` time granularity, save fixtures.
2. **Config and profile foundation** - pydantic-settings config (hard `SCAN_INTERVAL_MIN >= 15`), `ProfileService` loading `profile.md` + `constraints.yaml` and computing `profile_hash`, domain errors + `assert_defined`.
3. **Data model and migrations** - SQLAlchemy 2.0 entities and Alembic async migrations plus repository methods.
4. **Scraper ingestion** - politeness httpx client, centralized selectors, detail fetch only for new listings, watermark pagination, normalization.
5. **Freshness gate and hard rules** - analysis-window logic and the deterministic 0-token rule engine over `constraints.yaml`.
6. **LLM matching** - versioned prompt, OpenAI `.parse()` against `MatchVerdict`, evaluation persistence, threshold decision, retry + `llm_error` fallback.
7. **Telegram notification** - lean Bot API client, compact HTML match message, digest, `notified_at` only after a successful send, `test-notify`.
8. **Scheduler and pipeline runner** - `AsyncIOScheduler` 15-min loop, seed-run detection, orchestration of stages 0-3, per-listing isolation, `runs` protocol, cron-friendly `run-once`.
9. **Resilience and self-monitoring** - tenacity retry (never on 403), 403/captcha cooldown, consecutive-failure warning.
10. **Reporting basis** - verdict distribution, matches per day, top no-match reasons, token cost, via the `stats` command.
11. **Docker and home-server deployment** - multi-stage image, `compose.yaml` (app + postgres + volume), healthcheck, migrations on start.
12. **README and legal** - setup, operation, threshold tuning, troubleshooting, and the compliance finding.

Post-MVP (not built now): 13 Telegram bot commands, 14 re-evaluation with a new profile, 15 queue-based multi-source workers.

## Core rules (binding, from SPEC section 3)

Two separate guarantees drive the whole design:

- **Lossless DB (completeness).** `source_state` holds a **watermark** (timestamp of the last successful run). Each run paginates the "newest first" search URLs until it only sees known `url_hash` values or entries older than the watermark, so every gap (failure, restart, downtime) is closed on the next run and every listing ever seen lands in `listings`.
- **Seed run.** On an empty DB the full current inventory is persisted as a reporting baseline with status `skipped_stale` and **zero notifications**.
- **Analysis only for fresh entries.** `ANALYSIS_WINDOW_MIN` (default 30, = interval x 2) gates evaluation. Freshness signal order: (1) `posted_at` if minute-precise, else (2) gap rule (distance to last successful run <= window). Older new entries are stored `skipped_stale` with a reason JSON. Feature 1 verifies the real time granularity and decides the implementation.
- **Evaluation pipeline per new, fresh entry.** Stage 0 dedupe by `url_hash` (known -> only update `last_seen_at`); Stage 1 freshness gate; Stage 2 hard rules from `constraints.yaml` (0 tokens); Stage 3 LLM match against `profile.md` producing a structured `MatchVerdict`. A match with `score >= MATCH_THRESHOLD` (default 60) sends a Telegram message and sets `notified_at` after success.
- **Traceability.** Every entry gets a stored verdict with a reason for match **and** no-match, each `evaluations` row carrying `model`, `prompt_version`, `profile_hash`, token counts and latency.

## Data model

PostgreSQL, all timestamps `timestamptz` in UTC, enums as native PG enums, raw
payloads as JSONB. The profile is not stored in the DB (only its `profile_hash`
per evaluation).

### listings

- `id` (pk)
- `source` (str) - source key, e.g. `freelancermap`
- `external_url` (str, unique) - canonicalized listing URL
- `url_hash` (str, sha256, unique, indexed) - dedupe key
- `title`, `description` (str)
- `skills` (JSONB) - parsed skill tags
- `start_date` / `end_date` (date, nullable) - German formats parsed; `start_asap` flag for "ab sofort"
- `location` (str, nullable)
- `remote_status` (enum) - remote / hybrid / onsite / unknown
- `posted_at` (timestamptz, nullable) + `posted_at_precision` (enum: minute | day | unknown)
- `first_seen_at`, `last_seen_at` (timestamptz)
- `status` (enum: new | evaluated | skipped_stale)
- `notified_at` (timestamptz, nullable)
- `raw` (JSONB) - full parsed source record
- has many `evaluations`

### evaluations

- `id` (pk)
- `listing_id` (fk -> listings, **1:n** so a listing can be re-evaluated under a new profile)
- `stage` (enum: freshness | hard_rule | llm)
- `verdict` (enum: match | no_match | skipped_stale)
- `score` (int, nullable, 0..100)
- `reason` (JSONB) - stage-specific structure (rule + matched_term for hard rules; reasons/skills/gaps for LLM)
- `model`, `prompt_version`, `profile_hash` (str, nullable)
- `tokens_in`, `tokens_out`, `latency_ms` (int, nullable)
- `created_at` (timestamptz)

### runs

- `id` (pk)
- `started_at`, `finished_at` (timestamptz)
- `status` (enum: success | partial | error)
- `fetched`, `new`, `evaluated`, `matched`, `notified` (int counters)
- `error` (str, nullable)

### source_state

- `source` (pk, str)
- `watermark_at` (timestamptz, nullable) - last successful run boundary
- `cooldown_until` (timestamptz, nullable) - set on 403/captcha
- `consecutive_failures` (int)

> These shapes are locked; later features (ingestion, evaluation, reporting)
> depend on them. Change them through an Alembic migration, not ad hoc.

## Tech stack

- **Python 3.13, asyncio throughout** - async/await everywhere; blocking libraries are forbidden.
- **uv** - project, dependency, and command runner (`uv run ...`).
- **httpx (AsyncClient) + BeautifulSoup4/lxml** - HTTP and HTML parsing; Playwright only if Feature 1 proves JS rendering is required.
- **urllib.robotparser** - startup robots.txt gate including Crawl-delay.
- **SQLAlchemy 2.0 (typed) + asyncpg + Alembic** - typed ORM, async driver, checked-in async migrations on PostgreSQL.
- **APScheduler (AsyncIOScheduler)** - 15-min interval trigger with jitter, `max_instances=1`, `coalesce=True`.
- **pydantic v2 + pydantic-settings** - config parsing, `constraints.yaml` validation, and LLM output schemas.
- **OpenAI SDK** - `.parse()` with a Pydantic `response_format` for structured match verdicts; model from ENV.
- **tenacity** - retry with backoff on network/5xx/429, never on 403.
- **typer** - CLI: `init-db`, `run-once`, `daemon`, `test-notify`, `test-filter`, `stats`.
- **Telegram Bot API** - lean httpx client (`sendMessage`), no bot framework in the MVP.
- **pytest + pytest-asyncio + respx + pytest-cov** - fixtures, no live requests.
- **ruff + mypy --strict** - lint, format, and typing gate.
- **Docker + Compose** - containerized worker plus postgres on the home server.

## Monetization

Not in v1. Internal tool; the return is faster applications to matching listings
(won engagements) plus reference and learning value as a public Python project.

## UI/UX

No web UI. Telegram is the entire surface:

- **Match alert** - compact HTML message: title as link, score, two or three top reasons, start, location/remote.
- **Digest** - multiple matches in one run are batched.
- **Warnings** - one-time messages on source cooldown (403/captcha) and after consecutive failed runs.
- Display timezone is Europe/Berlin at output only; storage stays UTC.

## Open questions

> None blocking. Operational inputs Nik supplies at deploy time (profile content,
> search URLs, Telegram and OpenAI credentials, later threshold tuning) are
> tracked as open items in `SPEC.md` section 9 and the handover, not code gaps.
