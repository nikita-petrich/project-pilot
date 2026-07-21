# Project Plan

> project-pilot — personal project-listing pilot for freelancermap.de.
> Detailed rules (watermark/freshness semantics, compliance, data model) live in
> `SPEC.md` in the repo root and are binding for all features.

## 1. Problem - What problem are we solving?

New freelancermap listings must be reviewed within minutes, otherwise an application arrives too late. Manual checking does not scale; the platform's official "Projektagent" only mails once a day. project-pilot fetches new listings every 15 minutes, evaluates them automatically against Nik's profile, and reports only real matches immediately via Telegram — while losslessly persisting everything for later reporting. Secondary goal: build verifiable, idiomatic Python practice for Nik's AI-engineer positioning — code quality counts as a product property here.

## 2. Users - Who is this for?

Exactly one user: Nik (freelance full-stack & AI engineer). No multi-tenant, no registration, no public product.

## 3. Features - What does the MVP need?

- Moderate, robots.txt-compliant scraper for configured project board search URLs (public pages only, clear user agent, delays)
- Lossless ingestion: watermark-based pagination, dedupe via URL hash, everything is persisted
- Freshness gate: only entries within the analysis window (default 30 min) are evaluated; older ones → `skipped_stale` with a reason
- Two-stage evaluation: hard rules from `profile/constraints.yaml` (0 tokens), then LLM match against `profile/profile.md` via OpenAI structured output against a Pydantic model (verdict, score, reasons, matching skills, missing requirements)
- Reason + metadata are stored for match AND no-match as an `evaluations` row (model, prompt version, profile hash, tokens, latency)
- Telegram notification on match ≥ threshold (title, score, top reasons, start, location/remote, link)
- Scheduler at a 15-min interval with overlap protection, seed run without notifications, cooldown on 403/captcha
- Run logging (`runs`) and a simple reporting basis (verdict distribution, matches over time)

## 4. Data - What are we storing?

PostgreSQL: `listings` (all listings ever seen incl. raw data), `evaluations` (1:n per listing — verdict, score, reason JSON, model/prompt/profile metadata), `runs` (run protocol), `source_state` (watermark, cooldown, failure counter). The profile is NOT in the DB but versioned files `profile/profile.md` + `profile/constraints.yaml` in the repo; only the `profile_hash` is stored per evaluation.

## 5. Tech - What stack are we using?

Python 3.13, asyncio throughout · uv (pyproject, lockfile) · SQLAlchemy 2.0 typed + asyncpg + Alembic (async template) on PostgreSQL · APScheduler (AsyncIOScheduler) · httpx + BeautifulSoup4/lxml (Playwright only if JS rendering is proven) · urllib.robotparser · pydantic v2 + pydantic-settings (config, YAML, and LLM output validation) · OpenAI SDK with `.parse()` and Pydantic response_format · tenacity · typer CLI · Telegram via a lean httpx client against the Bot API · pytest + pytest-asyncio + respx with fixtures (no live requests) · ruff + mypy --strict · Docker + Compose.

## 6. Monetize - How will this make money?

Internal tool, no direct monetization. ROI = faster applications to matching listings → won freelance projects; additionally reference/learning value as a public Python project for the AI-engineer positioning.

## 7. UI/UX - How should this look and feel?

No web UI. Telegram is the interface: a compact HTML message per match (title as link, score, 2–3 reasons, start/location/remote), warning messages on cooldown/serial failures. Optional bot commands (`/stats`, `/pause`) are post-MVP.

## 8. Deployment - Where and how will this ship?

Docker container on Nik's own home server (not Render/Vercel — do not use `/release`). `docker compose` with two services: app + postgres (volume). Image: `python:3.13-slim`, uv-based multi-stage build, non-root; start via the typer CLI (`daemon`), Alembic migrations on start. ENV: DATABASE_URL, CONTACT_MAIL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY, LLM_MODEL, SCAN_INTERVAL_MIN (≥15), ANALYSIS_WINDOW_MIN, MATCH_THRESHOLD, SEARCH_URLS, LOG_LEVEL. Healthcheck: process liveness + last successful run < 3 × interval. No ingress/domain needed.
