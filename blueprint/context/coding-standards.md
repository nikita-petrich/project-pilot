# Coding Standards

> Conventions for Project Pilot: a TypeScript backend automation built on the
> Claude Agent SDK. No web UI, no React, no Next.js. The operator surface is a
> Telegram bot; the work is a 3-stage agent pipeline (Filter -> Research ->
> Application) driven by a job queue over PostgreSQL.
>
> Some sections carry `> TODO` markers where a decision is still open (queue
> library, project layout). Resolve them as the code lands and update this file.

## TypeScript

- Strict mode enabled
- No `any` types - use proper typing or `unknown`
- Define interfaces for all agent inputs/outputs, external API responses, and
  data models
- Use type inference where obvious, explicit types where helpful
- Prefer discriminated unions for the pipeline stages and record statuses over
  loose string fields

## Agent Pipeline (Claude Agent SDK)

- Three stages run in order per lead: **Filter -> Research -> Application**. Keep
  each stage a distinct, independently testable unit; a stage takes a typed input
  and returns a typed result plus a status transition.
- Default to `claude-opus-4-8` for agent calls unless a stage is explicitly
  chosen for a cheaper tier. Don't downgrade a model for cost without a decision.
- Every agent boundary (prompt inputs, tool arguments, structured outputs) is
  validated with Zod. Treat model output as untrusted until parsed.
- Keep prompts and their few-shot examples in versioned files, not inlined as
  giant string literals scattered through logic.
- The categorization step (autonomous vs. approval-required) is pure, testable
  logic driven by configurable thresholds - not a model call. Keep the thresholds
  in one config module.

## Sourcing & Integrations

- Each source (freelancermap, freelance.de, Malt IMAP, recruiter email) is an
  adapter that normalizes to one internal `Project` shape. Adapters never leak
  source-specific fields downstream.
- Scraping goes through Apify; wrap it behind an interface so a source can be
  swapped or mocked in tests.
- External calls (Apify, Telegram, LinkedIn, email, Notion, Claude) are isolated
  behind thin client modules. Business logic depends on the interface, not the
  vendor SDK directly, so each is mockable.
- Rate-limit-sensitive integrations (LinkedIn connect automation especially) must
  centralize their throttling in one place, not sprinkle sleeps through callers.

## Job Queue & Orchestration

- Work runs as queued, retryable jobs - not long inline request handlers.
  > TODO: queue library not yet chosen (pg-boss vs. BullMQ vs. Inngest). Pick one,
  > then document the job-definition and retry conventions here.
- Jobs are idempotent where possible; a retried job must not double-send an
  application or duplicate a Notion entry.
- Human-in-the-loop gates (Telegram approval) are modeled as explicit state, not
  blocking waits. A lead sits in `awaiting approval` until an approval event
  advances it.

## File Organization

> TODO: confirm once the app is scaffolded. Proposed layout:

- Agents/stages: `src/agents/[stage].ts` (filter, research, application)
- Integration clients: `src/integrations/[service].ts` (apify, telegram, linkedin,
  email, notion)
- Source adapters: `src/sourcing/[source].ts`
- Jobs/workers: `src/jobs/[job].ts`
- Domain types: `src/types/[domain].ts`
- Config: `src/config/`
- DB access: `src/db/`
- Shared utils: `src/lib/[utility].ts`

## Naming

- Files: kebab-case
- Functions: camelCase
- Constants: SCREAMING_SNAKE_CASE
- Types/Interfaces: PascalCase (no prefix)
- Job names and record statuses: stable, lowercase, hyphen-or-underscore
  identifiers used consistently across DB and code

## Database

- PostgreSQL on Neon.
  > TODO: confirm access layer (ORM vs. query builder vs. raw). Whatever is
  > chosen, document the migration workflow here and require migrations to be
  > checked in and applied before deploy (never ad-hoc schema edits in prod).
- Schema changes go through checked-in migrations; verify migration status before
  committing.
- Store timestamps in UTC. Persist enough of each lead's history to resume the
  pipeline after a restart (status, score, category, channel outcomes).

## Data Validation & Error Handling

- Validate all external inputs (source payloads, webhook/Telegram events, model
  output) with Zod at the boundary.
- Wrap fallible integration calls in try/catch and surface a typed result rather
  than throwing across module boundaries; return a `{ success, data, error }`-style
  result where a caller must branch on failure.
- Log failures with enough context to trace a single lead through the pipeline
  (a correlation/lead id on every log line for that lead).
- Never let one failed lead abort a batch; isolate and record it, continue the rest.
- Secrets (API keys, IMAP creds, tokens) come from environment/secret storage,
  never hardcoded and never committed.

## Testing

The blueprint installs no test runner; testing is opt-in at the project level.
Adding unit testing is an explicit setup task done through the normal workflow,
either as a build-plan item or with `/tests`. The setup chooses the stack-native
runner, wires the scripts, adds a small example test, and updates the Commands
section of `AGENTS.md`.

**The opt-in switch is one signal: a `test` command in the Commands section of
`AGENTS.md`.** Declare one and **tests become a gate for logic-bearing steps**;
leave it out and the loop verifies logic with the evidence it already uses (run
it, sample output, the build). Adding the runner is itself a deliberate step,
never a silent mid-step install.

- **What to test (the scope rule):** pure logic where a wrong answer is possible -
  source adapters/normalizers, the filter and categorization logic, score
  thresholds, cover-letter/reference selection, contact parsing, id/status
  builders. These have assertable inputs and outputs and real edge cases (empty,
  missing, malformed source payloads).
- **What not to test with unit tests:** live integration surfaces - real Apify
  scrapes, real Telegram/LinkedIn/email/Notion calls, real Claude calls. Mock the
  client and assert on how it's called; verify end-to-end behavior by running the
  pipeline against fixtures.
- **The gate (when a runner is configured):** a build step that adds in-scope logic
  must ship a passing test in the same reviewable diff. The test command must be
  green before the step is approved, before any checkpoint commit, and before
  `/complete` merges. Integration-only wiring steps ride on run-output plus build
  evidence.
- **When it's named:** the `/feature` spec's Testing section predicts the coverage,
  `/implement` writes the test with the step, and if a step surfaces logic the spec
  didn't foresee, add a focused test then.
- An empty suite should fail, not pass, so "no tests ran" never looks like "passed".
- Test files live next to source files (for example `filter.test.ts`).
- Run them via the project's test command (see Commands in `AGENTS.md`), not a
  hardcoded tool name.

Stack binding: a TypeScript app uses Vitest, `vi.mock()` for external
dependencies (the integration clients, the DB, Claude), and `vi.useFakeTimers()`
for time-dependent logic (throttling, scheduling, retries).

## Verification

There is no browser to screenshot. Verify behavior by running the relevant piece
and observing real output:

- Run a stage or job against fixture leads and inspect the produced status
  transitions, scores, and generated text.
- For integration wiring, use each vendor's sandbox/test mode where available
  (e.g. a test Telegram chat) rather than firing at real recipients.
- Guard destructive/outward actions (sending applications, LinkedIn connects,
  Notion writes) behind a dry-run mode so flows can be exercised without side
  effects during development.

## Code Quality

- No commented-out code unless specified
- No unused imports or variables
- Keep functions under 50 lines when possible

## Comments

Write code that explains itself; comment only what the code cannot say.
Over-commenting is a common AI tell, so resist it.

- Comment the **why**, not the **what**. Delete any comment that restates the code.
- No banner/header blocks, section dividers, or step-by-step narration of obvious
  code. A file does not need a comment announcing each region.
- A comment earns its place only when it captures something the code can't: a
  non-obvious decision, a gotcha or workaround, why a value is what it is, or a
  link to a spec or issue.
- Prefer self-documenting names and small functions over explanatory comments.
- Keep doc comments minimal: a one-line purpose on an exported type or function is
  plenty; don't write JSDoc that just repeats the signature.
- When in doubt, leave the comment out.

## Writing

- No em dashes (U+2014) in generated content: docs, comments, commit messages,
  READMEs, specs. They read as AI-generated.
- Use a hyphen for `term - description` separators; rephrase prose with commas,
  parentheses, or a colon. Avoid en dashes and the ellipsis character too.
