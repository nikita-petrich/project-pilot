# project-pilot - Project Overview

> A personal, single-user worker that watches freelancermap.de, persists every
> listing losslessly, evaluates fresh ones against Nik's profile (hard rules then
> LLM), and pushes real matches within minutes: one Claude match-thread session
> per match, push via the Claude app. Backend only.
> Binding detail spec: `SPEC.md` at the repo root (Telegram references there are
> historical — notification moved to Slack with feature 17 and to Claude
> match-thread sessions with features 19–23; Slack has been removed).

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
receives Claude match-thread pushes, and tunes the match threshold over time.

## Features

Build-plan order (MVP 1-12). The headline is the evaluation-plus-alert loop
(features 5-8).

1. **Compliance check and source verification** - snapshot robots.txt and ToS, prove go/no-go, determine initial-HTML vs JS rendering and `posted_at` time granularity, save fixtures.
2. **Config and profile foundation** - pydantic-settings config (hard `SCAN_INTERVAL_MIN >= 15`), `ProfileService` loading `profile.md` + `constraints.yaml` and computing `profile_hash`, domain errors + `assert_defined`.
3. **Data model and migrations** - SQLAlchemy 2.0 entities and Alembic async migrations plus repository methods.
4. **Scraper ingestion** - politeness httpx client, centralized selectors, detail fetch only for new listings, watermark pagination, normalization.
5. **Freshness gate and hard rules** - analysis-window logic and the deterministic 0-token rule engine over `constraints.yaml`.
6. **LLM matching** - versioned prompt, OpenAI `.parse()` against `MatchVerdict`, evaluation persistence, threshold decision, retry + `llm_error` fallback.
7. **Match notification** - compact match message, `notified_at` only after a successful send, `test-notify`. (Built as Telegram; replaced by Slack in feature 17, by Claude match-thread sessions in features 22–23.)
8. **Scheduler and pipeline runner** - `AsyncIOScheduler` 15-min loop, seed-run detection, orchestration of stages 0-3, per-listing isolation, `runs` protocol, cron-friendly `run-once`.
9. **Resilience and self-monitoring** - tenacity retry (never on 403), 403/captcha cooldown, consecutive-failure warning.
10. **Reporting basis** - verdict distribution, matches per day, top no-match reasons, token cost, via the `stats` command.
11. **Docker and home-server deployment** - multi-stage image, `compose.yaml` (app + postgres + volume), healthcheck, migrations on start.
12. **README and legal** - setup, operation, threshold tuning, troubleshooting, and the compliance finding.

Shipped post-MVP: 16 application autopilot (LLM draft + reviewed SMTP send, `applications` table), 17 Slack replaces Telegram (Block Kit messages, Socket Mode bot with Apply/`/apply`/`/check`/thread review), 18 opt-in contact enrichment (company-website lookup, `contact_leads` table).

Not built: 13 bot commands (`/stats`, `/pause`, …), 14 re-evaluation with a new profile, 15 queue-based multi-source workers.

## Core rules (binding, from SPEC section 3)

Two separate guarantees drive the whole design:

- **Lossless DB (completeness).** `source_state` holds a **watermark** (timestamp of the last successful run). Each run paginates the "newest first" search URLs until it only sees known `url_hash` values or entries older than the watermark, so every gap (failure, restart, downtime) is closed on the next run and every listing ever seen lands in `listings`.
- **Seed run.** On an empty DB the full current inventory is persisted as a reporting baseline with status `skipped_stale` and **zero notifications**.
- **Analysis only for fresh entries.** `ANALYSIS_WINDOW_MIN` (default 30, = interval x 2) gates evaluation. Freshness signal order: (1) `posted_at` if minute-precise, else (2) gap rule (distance to last successful run <= window). Older new entries are stored `skipped_stale` with a reason JSON. Feature 1 verifies the real time granularity and decides the implementation.
- **Evaluation pipeline per new, fresh entry.** Stage 0 dedupe by `url_hash` (known -> only update `last_seen_at`); Stage 1 freshness gate; Stage 2 hard rules from `constraints.yaml` (0 tokens); Stage 3 LLM match against `profile.md` producing a structured `MatchVerdict`. A match with `score >= MATCH_THRESHOLD` (default 60) sends a Telegram card with three buttons (open the listing · accept, which drafts the application at once · decline, which deletes the card) and sets `notified_at` after success.
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

`listings.origin` (enum: scan | chat | mail | pdf | image | url | api) records how
a row got there: the scanner's own listings are `scan`, everything the MCP
`ingest_listing` tool stores names its channel, with the detail in `raw["ingest"]`.
`listings.source` names the platform alongside it (`freelancermap`, `linkedin`, an
agency, `manual`), read off the listing URL when not passed in.

**Single-source only where it has to be.** The scraper is freelancermap-specific
by design (its parser, `SEARCH_URLS`, the watermark in `source_state`); the data
model, the evaluation prompts, the application flow and the whole MCP surface are
not, and carry no board name. A second platform therefore reaches the database
today through `ingest_listing` (mail or n8n, no code), and *scanning* one is a new
parser behind the same pipeline — build-plan item 15.

Features 16 and 18 added two further tables: `applications` (one draft/send cycle
per application: recipient, subject/body, LinkedIn message, status guard against
double sends, token accounting) and `contact_leads` (append-only enrichment
lookups: e-mails, phones, persons, research links).

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
- **Telegram + MCP (FastMCP)** - the worker sends every match itself (one retried HTTP POST, send-only: no polling, no webhook, no inbound port); the message's button opens the Claude project that collects the match chats, and an MCP server exposes feed, checks, drafts and send to Claude chats and n8n — including the workflow prompts, so one definition serves every surface.
- **pytest + pytest-asyncio + respx + pytest-cov** - fixtures, no live requests.
- **ruff + mypy --strict** - lint, format, and typing gate.
- **Docker + Compose** - containerized worker plus postgres on the home server.

## Monetization

Not in v1. Internal tool; the return is faster applications to matching listings
(won engagements) plus reference and learning value as a public Python project.

## UI/UX

No web UI of its own. The Claude app is the entire surface:

- **Match alert** - a Telegram card carries the whole listing to phone and desktop within seconds (the desktop app notifies with nothing open). Accepting drafts the application immediately and points into the Claude project, where the account skills and the MCP tools finish it; declining deletes the card.
- **Application flow** - drafting, revisions, recipient handling and the human-confirmed send happen in the match's session via the MCP tools (`draft`, `revise`, `set_recipient`, `send`).
- **Warnings** - source cooldown (403/captcha), LLM health, and consecutive-failure warnings arrive as their own plain-text sessions over the same channel.
- Display timezone is Europe/Berlin at output only; storage stays UTC.

## Open questions

> None blocking. Operational inputs Nik supplies at deploy time (profile content,
> search URLs, Telegram/MCP and OpenAI credentials, later threshold tuning) are
> tracked as open items in `SPEC.md` section 9 and the handover, not code gaps.
