# AGENTS.md

Instructions for AI coding agents working in this project. This is the cross-tool
entry point: Codex, Cursor, GitHub Copilot, Gemini CLI, Aider, Zed, Windsurf, and
others read `AGENTS.md`. Claude Code reads `CLAUDE.md`, which imports this file, so
there is a single source of truth.

## What this is

project-pilot is a personal, single-user worker that watches freelancermap.de for
new project listings, persists every listing losslessly in PostgreSQL, evaluates
fresh ones against Nik's profile (deterministic hard rules, then an LLM match),
and pushes real matches to Slack within minutes. Backend only, no web UI. The
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

App (typer CLI, entry point `project_pilot.cli:app`):

- Initialize DB schema: `uv run project-pilot init-db`
- Single scan, cron-friendly (non-zero exit on a failed run): `uv run project-pilot run-once`
- Scheduler daemon (scan loop + Slack bot): `uv run project-pilot daemon`
- Slack bot only (Apply buttons, `/apply`, `/check`, thread review): `uv run project-pilot bot`
- Post a test Slack message: `uv run project-pilot test-notify`
- Dry-run the filter against a listing: `uv run project-pilot test-filter`
- Find a company's contact data (opt-in `ENRICHMENT_ENABLED`): `uv run project-pilot enrich "<company>"` or `enrich --listing-id <id>`
- Reporting summary: `uv run project-pilot stats`

Database migrations (Alembic, async template): `uv run alembic upgrade head`

Local dev Postgres: `docker compose -f compose.dev.yaml up -d`, or an equivalent
local PostgreSQL 16 on `localhost:5432` matching `DATABASE_URL`.

Container image (Feature 11): `docker build -t project-pilot .`

Deployment: pushing to `main` runs the quality gate, builds the image into GHCR, and
deploys to the VPS at `/opt/stacks/project-pilot` over SSH
(`.github/workflows/deploy.yml`, see `docs/deployment.md`). The same gate runs on
branches and PRs via `.github/workflows/ci.yml`. `profile/profile.md` and the CVs in
`cv/` are versioned and ride inside the image, so updating either is a commit; the
app's `.env` comes from the `PROJECT_PILOT_ENV` secret in the `prod` environment and
is written to the server on every deploy. The server holds no configuration of its own.

Testing is ON: the `Test: uv run pytest` command above is the opt-in switch, so a
build step that adds logic-bearing code ships a passing test in the same diff and
the suite must be green before the step is approved (see `coding-standards.md`).
