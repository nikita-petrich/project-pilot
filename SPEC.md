# project-pilot — Specification (v3, Python)

**What this is:** Binding detail specification for building *project-pilot*. Feature execution runs through the ai-blueprint workflow; `blueprint/project-plan.md`, `blueprint/build-plan.md`, and `blueprint/context/coding-standards.md` are the workflow inputs, this document provides the detailed rules behind them.

**Status:** v3.1 (final). Domain cornerstones: Telegram-only, no Apify, 15-minute interval, lossless persistence of all listings, `evaluations` with a stored reason for match **and** no-match, LLM matching against Nik's profile, watermark/freshness semantics.

> **Addendum (2026-08):** the shipped system has since moved past this spec in
> three places (see `blueprint/build-plan.md` 16–23). Notification is neither
> Telegram nor Slack any more: every match fires the **Claude `match-thread`
> routine**, which opens one Claude session per match and pushes it through the
> Claude app, and an **MCP server** exposes every function to Claude chats and
> n8n — so every Telegram reference below, and the Slack addendum that replaced
> it, are historical (`docs/claude-setup.md` is the live wiring). An
> application-drafting flow (LLM draft, reviewed SMTP send, `applications` table)
> and opt-in contact enrichment (`contact_leads` table) were added. The
> watermark/freshness/lossless rules in §3–5 remain binding.

**Why Python (context for /overview and all feature specs):** Nik positions himself as an AI engineer; Python is on his CV and LinkedIn tagline, and the hands-on Python experience is to be built verifiably with this project. project-pilot is deliberately also a learning vehicle: the code should demonstrate idiomatic, modern Python (Pydantic v2, SQLAlchemy 2.0 typed, asyncio, structured outputs) — quality over speed.

---

## 1. Stack

**Python 3.13 · asyncio throughout · uv as package/project manager · PostgreSQL — as a long-running worker in a Docker container on Nik's server.**

| Component | Choice | Note |
|---|---|---|
| Runtime model | asyncio (async/await throughout) | blocking libs (e.g. `requests`) are forbidden |
| Project/deps | `uv` (pyproject.toml, lockfile) | `uv run` for all commands |
| Config | `pydantic-settings` | ENV parsing with validation at boot, fail fast |
| HTTP client | `httpx` (AsyncClient) | timeouts, connection limits, testable via `respx` |
| HTML parsing | `BeautifulSoup4` + `lxml` parser | Playwright **only** if Feature 1 proves JS rendering |
| robots.txt | `urllib.robotparser` (stdlib) | checked at startup incl. Crawl-delay; violation = startup abort |
| ORM | SQLAlchemy 2.0 (typed, `Mapped[...]`) + `asyncpg` | `postgresql+asyncpg://`; migrations exclusively via Alembic (async template) |
| Scheduling | APScheduler `AsyncIOScheduler` | interval trigger with jitter, `max_instances=1`, `coalesce=True` |
| Retry | `tenacity` | exponential backoff + jitter, only network errors/5xx/429 — never 403 |
| Telegram | own lean client via `httpx` against the Bot API (`sendMessage`) | no bot framework in the MVP; `aiogram` later for post-MVP commands |
| LLM | OpenAI Python SDK, small inexpensive model, `.parse()` with a **Pydantic model as response_format** | structured outputs natively validated against Pydantic |
| CLI | `typer` | typed commands: init-db, run-once, daemon, test-notify, test-filter, stats |
| Tests | `pytest` + `pytest-asyncio` + `respx` + `pytest-cov` | no live requests; HTML/JSON fixtures |
| Quality | `ruff` (lint **and** format) + `mypy --strict` | gate before every `/check` |

## 2. Profile Storage

No Redis, no DB — versioned files in the repo, loaded into an in-memory object at boot:

```
profile/
├── profile.md          # free-text profile (goes into the LLM prompt): skills, experience,
│                       # desired projects, no-gos in prose
└── constraints.yaml    # hard, deterministic rules (stage 2, no LLM):
                        # blacklist terms, must_have (e.g. remote), languages,
                        # nogo_technologies (stage 3 guard, see below)
```

A `ProfileService` loads both at startup, validates `constraints.yaml` via Pydantic, and computes `profile_hash` (SHA-256) — the hash is **stored on every evaluation**, so reporting can later tell which profile version produced which verdict. Changing the profile = edit file, commit, restart container.

## 3. Core Domain Rules (the "nothing gets lost" semantics)

Two separate guarantees:

**A — Completeness of the DB (lossless from project start):**
- `source_state` holds a **watermark**: timestamp of the last successful run.
- Every run paginates the search URLs (sorted "newest first") until only known entries (`url_hash` in DB) or entries older than the watermark appear. The next run thus closes every gap (failure, restart, downtime) — **every listing ever seen lands in `listings`**.
- **Seed run** (empty DB): the complete current inventory is persisted (reporting baseline), status `skipped_stale`, **zero notifications**.

**B — Analysis only for fresh entries:**
- `ANALYSIS_WINDOW_MIN` (default **30** = interval × 2): only newly seen entries within the window go through evaluation + possibly Telegram. Older new entries are persisted with status `skipped_stale` + reason metadata (`{"reason": "posted_at older than analysis window", "posted_at": …, "gap_minutes": …}`).
- Freshness signal, in this order: (1) `posted_at`, if available with minute precision; (2) otherwise gap rule: distance to the last successful run ≤ window ⇒ fresh. **Feature 1 verifies freelancermap's actual time granularity** — the result decides the implementation.

**Evaluation pipeline per new, fresh entry:**

```
Stage 0  Dedupe (url_hash)                     → known: only update last_seen_at
Stage 1  Freshness gate                        → skipped_stale + reason JSON
Stage 2  Hard rules (constraints.yaml)         → verdict no_match, reason: {rule, matched_term}   (0 tokens)
Stage 3  LLM match against profile.md          → structured output (Pydantic model):
         { verdict: match|no_match, score: 0..100, reasons: list[str],
           matching_skills: list[str], missing_requirements: list[str], risk_flags: list[str] }
         + stored: model, prompt_version, profile_hash, tokens_in/out, latency_ms
Stage 3b No-go guard (nogo_technologies)       → a no-go term reported by the model itself under
         missing_requirements forces no_match (score 0, reason: {nogo}). The profile's technology
         no-gos are context-dependent, so they cannot be blacklisted on the listing text; the
         model's own "the listing requires it and the profile does not cover it" is the signal.
Match ∧ score ≥ MATCH_THRESHOLD (default 60)   → Telegram message, notified_at after success
```

This gives **every** entry a traceable verdict with a reason — match and no-match alike — as the basis for later reporting.

## 4. Data Model (SQLAlchemy entities, short spec)

- **`listings`** — id, source, external_url (canonicalized, unique), url_hash (sha256, unique, indexed), title, description, skills (JSONB), start_date/end_date (nullable; German source formats parsed, e.g. "ab sofort" → start_asap flag, "01.09.2026", "keine Angabe"), location, remote_status (enum), posted_at (nullable) + posted_at_precision (minute | day | unknown), first_seen_at, last_seen_at, status (enum: new | evaluated | skipped_stale), notified_at (nullable), raw (JSONB).
- **`evaluations`** — id, listing_id (FK, **1:n** — re-evaluation with a new profile possible), stage (hard_rule | llm | freshness), verdict (match | no_match | skipped_stale), score (nullable), reason (JSONB — structure from stages 2/3), model, prompt_version, profile_hash, tokens_in, tokens_out, latency_ms, created_at.
- **`runs`** — id, started_at, finished_at, status (success | partial | error), fetched, new, evaluated, matched, notified, error (nullable).
- **`source_state`** — source (PK), watermark_at, cooldown_until (nullable), consecutive_failures.

All timestamps `timestamptz` in UTC; enums as native PostgreSQL enums.

## 5. Hard Guardrails (MUST)

1. Only pages publicly reachable without login; no login bypass, no captcha/bot-protection circumvention, no disguise (UA/proxy rotation).
2. Check `robots.txt` at startup via `urllib.robotparser` (incl. Crawl-delay); disallowed path ⇒ startup abort with a message.
3. Interval config hard-validated: `>= 15` minutes. Operating value: **15**.
4. User agent: `project-pilot/1.0 (personal project alert bot; contact: <MAIL_FROM_ENV>)`.
5. Per run: max. 2 list pages per search URL (watermark pagination stops early anyway) + detail pages **only for new** entries, 2–5 s random delay between requests.
6. HTTP 403 / captcha indicator ⇒ abort the run, `cooldown_until = now + 6h`, one-time Telegram warning. No retry hammering.
7. Secrets only via ENV (`.env` locally, never in the repo); `.env.example` fully maintained.
8. **STOP conditions** (agent halts, documents, asks Nik): robots.txt disallows the project board paths · ToS explicitly prohibit automated access · captcha even on moderate single requests · any ambiguity that would water down rules 1–7.

Research result (July 2026): no public read API, RSS discontinued, the official "Projektagent" only mails daily — hence an own moderate scraper.

## 6. Project Structure

```
project-pilot/
├── pyproject.toml              # uv-managed; ruff, mypy (strict), pytest configured
├── .python-version             # 3.13
├── .env.example
├── profile/
│   ├── profile.md              # Nik fills in content
│   └── constraints.yaml        # Nik fills in content
├── docs/
│   ├── compliance.md           # robots.txt/ToS snapshot from Feature 1
│   └── adr/
├── alembic/                    # async template
├── src/project_pilot/
│   ├── __init__.py
│   ├── cli.py                  # typer: init-db | run-once | daemon | test-notify | test-filter | stats
│   ├── config.py               # pydantic-settings (SCAN_INTERVAL_MIN >= 15 hard)
│   ├── db.py                   # async engine/session factory
│   ├── models.py               # SQLAlchemy 2.0 entities
│   ├── repository.py           # get_known_hashes, upsert_listing, record_run, …
│   ├── profile_loader.py       # ProfileService + profile_hash
│   ├── errors.py               # SourceBlockedError, SelectorMismatchError, LlmSchemaError, assert_defined
│   ├── ingestion/
│   │   ├── client.py           # politeness httpx client (UA, robots gate, delays, timeouts)
│   │   ├── parser.py           # selectors centralized as a constants block
│   │   ├── normalize.py        # URL canonicalization, German date formats, remote heuristic
│   │   └── watermark.py        # pagination stop criterion
│   ├── evaluation/
│   │   ├── schemas.py          # Pydantic: MatchVerdict, RuleResult
│   │   ├── rules.py            # stage 2 (constraints.yaml), word boundaries incl. c#/c++/.net
│   │   ├── llm.py              # stage 3: OpenAI .parse() against MatchVerdict
│   │   ├── nogo.py             # stage 3b: required-no-go guard over missing_requirements
│   │   └── prompts/match.v1.md
│   ├── notification/telegram.py
│   ├── pipeline.py             # orchestration stages 0–3 + runs protocol
│   ├── scheduler.py            # AsyncIOScheduler daemon
│   └── reporting.py
└── tests/
    ├── fixtures/               # saved HTML/JSON/LLM snapshots
    └── test_*.py
```

## 7. ENV Variables (`.env.example`)

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://…` (locally via compose.dev.yaml, prod on the server) |
| `CONTACT_MAIL` | inserted into the user agent |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | create the bot via @BotFather; chat ID of your own conversation |
| `OPENAI_API_KEY` | LLM matching |
| `LLM_MODEL` | small mini model; deliberately ENV, not hardcoded |
| `SCAN_INTERVAL_MIN` | default 15, validated ≥ 15 |
| `ANALYSIS_WINDOW_MIN` | default 30 |
| `MATCH_THRESHOLD` | default 60 |
| `SEARCH_URLS` | comma-separated; searches Nik configured in the browser, sorted "newest first" |
| `LOG_LEVEL` | default `info` |

LLM cost frame: at ~50–150 fresh entries/day and ~1–2k tokens per evaluation with a mini model, a few cents per day; tokens are logged per evaluation.

## 8. Scaling Target (prepared, not built ahead)

Multi-platform later: one fetcher per source as an adapter (already cut that way), decoupling via a Redis queue (`arq`), stateless workers, shared pipeline. The queue boundary (JSON messages) keeps fetchers and pipeline cleanly decoupled. Anchored as post-MVP feature 15 in the build plan.

## 9. Open Items for Nik

1. Fill in `profile/profile.md` + `constraints.yaml` (skills, desired projects, no-gos, hard rules).
2. Assemble 1–3 search URLs on the project board and set them as `SEARCH_URLS`.
3. Create the Telegram bot via @BotFather, token + chat ID into `.env`.
4. Adjust `MATCH_THRESHOLD` after the first days based on the stored verdicts.
