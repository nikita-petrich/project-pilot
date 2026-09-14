# AGENTS.md

Instructions for AI coding agents working in this project. This is the cross-tool
entry point: Codex, Cursor, GitHub Copilot, Gemini CLI, Aider, Zed, Windsurf, and
others read `AGENTS.md`. Claude Code reads `CLAUDE.md`, which imports this file, so
there is a single source of truth.

## What this is

project-pilot is a personal, single-user worker that watches freelancermap.de for
new project listings, persists every listing losslessly in PostgreSQL, evaluates
fresh ones against Nik's profile (deterministic hard rules, then an LLM match),
and pushes real matches within minutes: the worker sends a Telegram card whose
buttons open the listing, open a new Claude chat with that card prefilled (a
`claude.ai/new?q=…` link), or decline the match; the chat works the match
through project-pilot's own MCP tools and skills, and the same MCP server
exposes every function to Claude chats and n8n.

**Only the scraper is freelancermap-specific** (its parser, `SEARCH_URLS`, the
watermark) — keep it that way. The data model, the evaluation prompts, the
application flow and the MCP tools carry no board name, so a listing from any
other platform reaches the database through `ingest_listing`, which records both
`listings.source` (which platform) and `listings.origin` (which channel).
Scanning a second board is a new parser behind the same pipeline, build-plan
item 15.
Backend only, no web UI of its own — the Claude app is the interaction surface
(see `blueprint/reference/zielarchitektur.drawio`). The
binding detail specification lives in `SPEC.md` at the repo root.

This project is built with the **AI Coding Blueprint**, a workflow layer, not an
app skeleton. To start a new project, scaffold the app first in an empty folder
(create-next-app, Vite, etc.), then overlay these files on top. Never run a
framework scaffolder inside a directory that already holds the blueprint files
(`AGENTS.md`, `CLAUDE.md`, `.agents/`, `.claude/`, `blueprint/`); it fails
because the directory isn't empty.

New here? `blueprint/README.md` explains the whole workflow.

## Read these for full context

- `blueprint/context/project-overview.md` - the project's source of truth
- `blueprint/context/coding-standards.md` - conventions to follow
- `blueprint/context/ai-interaction.md` - how to work with the user on this project
- `blueprint/context/current-feature.md` - the one feature or fix being built right now
- `blueprint/context/learnings.md` - patterns from real runs and the rule each one led to; check it before changing contact, drafting, sending or deploy behaviour, and add an entry when a new case turns up

## Workflow

Build one feature or fix at a time, behind review gates. Each step's instructions
are plain markdown skills any capable agent can read and follow. The workflow is
exposed through tool-specific adapters:

- Codex: `.agents/skills/<skill>/SKILL.md`
- Claude Code: `.claude/skills/<skill>/SKILL.md`

Unused adapters can be removed. Codex-only projects can delete `CLAUDE.md` and
`.claude/`. Claude Code-only projects can delete `.agents/`, but should keep
`AGENTS.md` because `CLAUDE.md` imports it.

When changing shared workflow behavior, update the matching skill in both
adapter folders so Codex and Claude Code stay aligned.

## Domain-Skills

Beyond the workflow skills below, two skills carry project-pilot's own judgment
into any Claude surface (Claude Code sessions today, any Claude chat via the account
architecture next):

- `/match-card` - show the full overview card for a stored listing (facts,
  verdict, research links), exactly as the Telegram alert renders it.
- `/check-project` - judge one project listing against Nik's profile, exactly as
  the scan pipeline does (stage-2 rules, then the verdict, then the no-go
  post-check on `missing_requirements`).
- `/write-application` - draft subject, body and LinkedIn message for a listing.
  Drafts only; sending stays with the human, by design.

All three are **thin wrappers**: `match-card` prints what
`project_pilot_match_card` renders, the other two read the canonical sources at
runtime
(`evaluation/prompts/match.v7.md`, `application/prompts/application.md`,
`profile/`) instead of copying their rules, so a skill and the pipeline cannot
drift apart. Change a judgment rule in the prompt file, not in the skill.

Core skills:

- `onboard` - tune commands, standards, visibility, ignore rules, and tool adapters after overlaying the Blueprint onto a freshly scaffolded or early project
- `doctor` - read-only Blueprint health check for setup, adapters, plans, overview freshness, and workflow drift
- `adopt` - bootstrap the Blueprint into an existing brownfield app with shipped features
- `overview` - distill the two planning docs into `blueprint/context/project-overview.md`
- `brief` - read-only briefing on an upcoming build-plan feature (scope, dependencies, size) before you spec it
- `feature` - turn a build-plan item into a spec in `blueprint/context/current-feature.md`
- `fix` - document an ad-hoc bug or change into `blueprint/context/current-feature.md`
- `tests` - add or normalize unit testing and turn on the test gate
- `implement` - build the current spec one small, reviewed step at a time
- `check` - prove the current spec against the running app
- `try` - read-only manual review guide: where to go, what to click, what to expect
- `audit` - read-only code quality review for duplication, dead code, standards drift, and maintainability risks
- `complete` - log it to `blueprint/history/features/` or `blueprint/history/fixes/`, then merge
- `prototype` - optional, pre-build static mockups to lock the look
- `status` - read-only progress summary, workflow drift warning, and suggested next action

In Codex, invoke these as skills (`$onboard`, `$overview`, `$feature`,
`$implement`, and so on) or ask naturally, such as "run the overview." In Claude
Code, use the slash commands (`/onboard`, `/overview`, `/feature`, and so on). In
tools without native skills, follow the matching `SKILL.md` manually. The
conventions in `blueprint/context/` apply however a step is invoked.

Optional explicit-only skill: `autopilot` can run one bounded spec/build/check
pass when directly invoked. It may create checkpoint commits on the feature or
fix branch after passing steps. It stops before `/complete`, merge, push, deploy,
or destructive actions.

## Commands

Python 3.13 worker managed with `uv`; every command runs through `uv run`. The
quality-gate commands work today; the `project-pilot` CLI subcommands come online
as their features land (see `blueprint/build-plan.md`).

Quality gate (all four must be green before every `/check`, checkpoint, and `/complete`):

- Lint: `uv run ruff check`
- Format check: `uv run ruff format --check`
- Types: `uv run mypy`
- Test: `uv run pytest`
- Judgment eval (golden set, real LLM calls — needs `LLM_MODEL` + the key of the
  configured `LLM_PROVIDER`,
  excluded from the normal suite): `uv run pytest -m eval`

App (typer CLI, entry point `project_pilot.cli:app`):

- Initialize DB schema: `uv run project-pilot init-db`
- Single scan, cron-friendly (non-zero exit on a failed run): `uv run project-pilot run-once`
- Scheduler daemon (scan loop; every match sends its Telegram card, whose
  Bewerben button is a prefilled Claude chat link — `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` are required):
  `uv run project-pilot daemon`
- Button poller (long polling, no inbound port; hears only the card's Ablehnen
  button and deletes the card — `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`):
  `uv run project-pilot telegram-bot`
- MCP server (Streamable HTTP + `MCP_TOKEN` bearer auth, for Claude connectors/n8n): `uv run project-pilot mcp`
- End-to-end smoke test (rules + LLM + a real push, stores nothing):
  `uv run project-pilot test-match`
  (`--text`/`--file` for your own description, `--listing-id N` for a stored listing)
- Dry-run the filter against a listing: `uv run project-pilot test-filter`
- Find a company's contact data: `uv run project-pilot enrich "<company>"` or `enrich --listing-id <id>`
- Reporting summary: `uv run project-pilot stats`

Database migrations (Alembic, async template): `uv run alembic upgrade head`

Local dev Postgres: `docker compose -f compose.dev.yaml up -d`, or an equivalent
local PostgreSQL 16 on `localhost:5432` matching `DATABASE_URL`.

Container image (Feature 11): `docker build -t project-pilot .`

Deployment: pushing to `main` runs the quality gate, builds the image into GHCR, and
deploys to the VPS at `/opt/stacks/project-pilot` over SSH
(`.github/workflows/deploy.yml`, see `docs/deployment.md`). The same gate runs on
branches and PRs via `.github/workflows/ci.yml`. The profile is **not** in the image: it is read from
sequenz.io at boot (the markdown twin `/<locale>.md` plus `/api/profile.json`), so
updating it is a website deploy, not a commit here. Only `profile/private.yaml` —
the no-go industries and technologies, which do not belong on a company homepage —
is versioned and rides inside the image; the CVs instead live in a public
Google Drive folder and are fetched by name before each send (see
`application/cv_drive.py`), so updating one is a Drive upload, no deploy. The app's
`.env` is rendered from the secrets and variables of the `prod` GitHub environment and
written to the server on every deploy, so adding a setting is a new secret rather than
a workflow change. The server holds no configuration of its own.

Testing is ON: the `Test: uv run pytest` command above is the opt-in switch, so a
build step that adds logic-bearing code ships a passing test in the same diff and
the suite must be green before the step is approved (see `coding-standards.md`).
