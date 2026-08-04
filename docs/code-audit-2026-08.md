# Code audit — August 2026

Full review of architecture, correctness, security, coding standards, tests, and
operations. Baseline at audit time (commit `7a06744`): all four quality gates green
(`ruff check`, `ruff format --check`, `mypy --strict`, `pytest`), 353 tests passing
against a real Postgres, total coverage 89 %.

Each finding lists the fix PR that addresses it, or **recommendation** when the fix
is an architectural change that should be decided explicitly rather than applied in
an audit sweep (per `ai-interaction.md`: ask before large refactors).

Severity: **critical** = data loss / main-loop crash on plausible input ·
**high** = real bug or compliance violation with a concrete failure scenario ·
**medium** = robustness or maintainability issue with plausible impact ·
**low** = minor.

## Correctness

### C1 — Watermark advances on partial runs, silently losing listings (critical)

`Pipeline._execute` runs `set_watermark(now)` unconditionally, but
`_fetch_and_store` swallows per-listing detail-fetch failures (including transient
`SourceUnavailableError`) and merely counts them. On the next run the failed
listing's `url_hash` is unknown, yet `evaluate_page` drops it because
`posted_at < watermark` — it is never fetched again. Worst case (connection drops
after the list pages): the whole scan window is lost while the run commits as
PARTIAL. This violates the binding lossless-DB rule (SPEC §3A).
**Fixed in PR "pipeline run integrity":** the watermark only advances on a run with
zero per-listing errors; the old watermark keeps the gap open and the known-hash
stop still bounds re-pagination.

### C2 — Captcha marker scan can false-positive on listing content (high)

`PolitenessClient.get` substring-scans every response body — including HTTP 200
detail pages — for markers like `hcaptcha`, `g-recaptcha`, `unusual traffic`. On a
developer job board a listing describing captcha/bot-protection work triggers
`SourceBlockedError`: run aborted, 6-hour cooldown, blocked warning — repeating
after every cooldown until the listing leaves the first two result pages.
**Fixed in PR "ingestion client":** a 200 response only counts as a captcha wall
when it also lacks the `js-react-on-rails-component` payload marker every real
freelancermap page carries; non-200 responses keep the plain marker check.

### C3 — Exhausted 429/5xx retries return the error page as content (high)

`_request` catches `_RetryableResponseError` after retries are exhausted and
returns the last 429/5xx response as if it were content — contradicting the class
docstring ("surface as `SourceUnavailableError`"). The error HTML then flows into
parsing: a list page fails as a generic `SelectorMismatchError` run failure, and a
detail page is swallowed per-listing — which, combined with C1, permanently lost
the listing.
**Fixed in PR "ingestion client":** exhausted retries on 429/5xx now raise
`SourceUnavailableError`; the run aborts cleanly and the watermark stays put.

### C4 — robots.txt gate never checks the `/projekt/` detail path (medium)

Startup validation covers only the configured search URLs plus the site root;
every detail fetch goes to `/projekt/<slug>`, a path never passed through
`can_fetch`. A robots change disallowing `/projekt/` would go unnoticed while the
scanner keeps fetching. (The Slack bot's `fetch_listing` does check the concrete
URL — the pipeline was the outlier.)
**Fixed in PR "ingestion client":** `get()` now consults the parser cached by
`check_robots` for every URL and raises `SourceBlockedError` on a disallowed path.

### C5 — robots.txt 403/5xx treated as allow-all (low)

`check_robots` mapped any robots response ≥ 400 to "no rules". A 403 on
robots.txt means the site is blocking this client — proceeding to crawl fails open
in exactly the wrong case (RFC 9309 treats unreachable robots as disallow).
**Fixed in PR "ingestion client":** 401/403 → `SourceBlockedError`; 5xx → surfaces
as `SourceUnavailableError` via C3's fix; only genuine 404/4xx means "no rules".

### C6 — On-site-only matches re-processed on every run forever (medium)

`_notify` skips an `onsite_only` match with a bare `continue`; `notified_at` stays
NULL, so `get_unnotified_matches` re-selects it (eagerly loading its evaluation
history) on every run for the lifetime of the database.
**Fixed in PR "pipeline run integrity":** the suppression is persisted — the
listing is marked handled on first skip and logged once.

### C7 — `MatchVerdict.score` unbounded despite the 0..100 contract (low)

The schema drops range constraints for OpenAI strict structured outputs and
nothing re-validates after parsing: a model returning 850 or −5 flows into the
threshold comparison, the DB, and Slack headers.
**Fixed in PR "standards & dead code":** score is clamped to 0..100 at the
consumption boundary (`LlmEvaluation`).

## Architecture

### A1 — One shared session defeats per-listing isolation (high)

The run is one session/transaction, but per-listing "isolation" is only a generic
`except Exception`. A DB-level flush failure (e.g. a scraped value exceeding a
column limit) poisons the session: every later repo call raises
`PendingRollbackError`, `finalize_run` (outside the try) raises out of
`run_once`, and everything rolls back — run row, stored listings, and the
`consecutive_failures` increment, so the 3-failures warning can never fire for
DB-type errors, and the crash repeats every scan.
**Fixed in PR "pipeline run integrity":** each listing's store/evaluate runs in a
savepoint (`begin_nested`), scraped `title`/`location` are truncated to their
column limits, and the run row is finalized in its own short session so run
bookkeeping survives a poisoned main session.

### A2 — Slack sends inside the run-long transaction (medium)

`send_match` (an external side effect) executed mid-transaction and
`mark_notified` only flushed; if the end-of-run commit failed, messages were
already delivered but `notified_at` and the evaluations rolled back — the next run
re-evaluates (paying tokens again) and re-sends duplicates. The codebase's own
pattern (`ApplicationService.send`) does it right.
**Fixed in PR "pipeline run integrity":** notification runs after the main unit of
work commits, in its own session, with a commit after each successful send.

### A3 — Cooldown makes the container healthcheck lie (low)

During a deliberate 403/captcha cooldown no run row is written for up to 6 hours,
and `is_healthy` requires a successful run within 45 minutes — so a by-design
protective pause reports the container unhealthy, indistinguishable from an
outage.
**Fixed in PR "pipeline run integrity":** `is_healthy` now also consults
`source_state` and reports healthy while a cooldown is active.

### A4 — Slash commands bypass the configured-channel guard (low)

`on_block_action` and the event handlers enforce `channel != self._channel`, but
`on_slash_apply`/`on_slash_check` accepted `channel_id` and never read it — a
command from any other channel was fully processed (tokens spent) and answered
into the configured channel.
**Fixed in PR "standards & dead code":** both commands return early for a foreign
channel, matching the documented guard.

### A5 — `LOG_LEVEL` validated but never applied (low)

Settings strictly validates `log_level`, but the only logging configuration
hardcodes `INFO` — `LOG_LEVEL=debug` validated successfully and silently did
nothing.
**Fixed in PR "standards & dead code":** every command applies the configured
level right after `load_settings()`.

### A6 — Dead code: four superseded normalize helpers (low)

`parse_start`, `parse_end`, `resolve_url`, and `remote_status_from_text` are
leftovers from the CSS-scraping era (the parser switched to react-on-rails JSON)
with no production caller — and `parse_start`/`parse_end` are unused twins of the
live `start_from_parts`, the riskiest kind of dead code.
**Fixed in PR "standards & dead code":** helpers and their tests removed.
`Repository.get_contact_leads` and `SchedulerRunner.has_job` are also
production-unused but are kept as deliberate small API surface (read-back of the
append-only lead trail; test seam) — flagged here for a conscious decision.

### A7 — Package-level dependency cycles (medium, recommendation)

`application ↔ evaluation` (via `ImageAttachment`/`build_user_content`) and
`evaluation ↔ notification` (via `MatchMessage`/`CheckResult`) close cycles at
package granularity. Nothing crashes today, but the domains no longer form a DAG
and any new cross-import along these edges becomes a real circular import.
**Recommendation:** move `ImageAttachment` and `build_user_content` into a shared
dependency-free module and let `CheckService` return domain data only, building
the `MatchMessage` in the notification layer. Mechanical but touches many
imports — worth its own reviewed change.

### A8 — `slack_bot.py` is an 830-line cross-domain orchestrator (medium, recommendation)

The bot dispatches envelopes, parses payload shapes, holds two in-memory pending
stores, and orchestrates four domains — runner-layer work living in
`notification`. Well-tested, so this is growth risk rather than a bug.
**Recommendation:** promote it to its own top-level package and split envelope
parsing, pending-state store, and flow routing.

### A9 — Two parallel politeness/robots stacks (medium, partly fixed)

Compliance-critical fetch logic exists twice (ingestion `PolitenessClient` vs.
enrichment `WebFetcher`/`RobotsGate`), and the enrichment side ignored a site's
`Crawl-delay` entirely while crawling third-party sites.
**Crawl-delay honoring fixed in PR "enrichment hardening"** (the gate now exposes
it and both enrichment fetchers respect `max(configured delay, crawl delay)`).
**Recommendation:** longer-term, migrate `PolitenessClient` onto the shared
`RobotsGate` so allow/deny + delay logic exists once.

### A10 — `ingestion/normalize.py` as cross-domain grab-bag (low, recommendation)

`detect_language`, `resolve_contact_name`, `looks_like_company`, and
`html_to_text` are consumed by application, enrichment, and notification —
domains importing from inside `ingestion` for functionality unrelated to
scraping. **Recommendation:** extract a shared text-utilities module.

### A11 — Binding docs still describe Telegram; whole domains undocumented (medium)

SPEC.md (v3.1 "final") and `blueprint/context/project-overview.md` — loaded into
every AI session as the source of truth — still say notifications go to Telegram
and know nothing of the application-drafting, enrichment, and check domains or
the `applications`/`contact_leads` tables. The code ships Slack.
**Fixed in this PR:** overview and coding-standards updated to Slack and the
shipped domains; SPEC.md carries an addendum note. A full SPEC rewrite (or
`/overview` re-run from updated plans) remains worthwhile.

## Security

The posture is genuinely good for a single-user tool: secrets only via ENV and
GitHub environments, `.env` written 0600 over stdin on deploy, non-root container
user, immutable image tags, EmailMessage header folding handled, recipient
addresses regex-validated before SMTP, Slack mrkdwn escaped via `_esc`, no SQL
outside the ORM. Findings:

### S1 — Enrichment CLI bypasses the `ENRICHMENT_ENABLED` opt-in (medium)

The feature contract says opt-in, "never makes surprise outbound calls" — the bot
honors it, but `project-pilot enrich` built the service and made outbound
search/fetch calls regardless of the flag.
**Fixed in PR "enrichment hardening":** the command now requires
`ENRICHMENT_ENABLED=true` and exits with a hint otherwise.

### S2 — `WebFetcher` fetches unvalidated targets with no limits (medium)

Enrichment fetches URLs coming from web search results and listing data with no
scheme allowlist, no guard against literal private/loopback/link-local addresses
(SSRF exposure toward the home network the worker runs in), no response-size cap,
and no content-type check before HTML parsing.
**Fixed in PR "enrichment hardening":** http(s)-only, private/reserved IP
literals refused, responses capped, non-text content skipped. (A DNS-resolution
pinning guard was deliberately left out as disproportionate for this deployment;
noted here for completeness.)

### S3 — DuckDuckGo redirect unwrapping skips scheme validation (low)

`_unwrap_ddg` validated the scheme of direct hrefs but returned the `uddg=`
redirect target unvalidated. **Fixed in PR "enrichment hardening".**

### S4 — Bare `assert isinstance` in production path (low)

`run_socket_mode` used two bare asserts (stripped under `-O`; forbidden by
coding-standards). **Fixed in PR "standards & dead code"** via typed parameters.

### S5 — Prompt injection via listing text (informational)

Scraped listing text flows into the match prompt; a hostile listing could skew
its own verdict or the generated draft. Contained by design: structured outputs
constrain the response shape, the verdict only affects a notification to Nik, and
a human reviews every draft before sending. No change made; keep the human review
step.

## Tests & CI

- CI runs all four gates plus `alembic upgrade head` against a real Postgres
  service — the 58 locally-skipped DB tests do run in CI. A forked migration
  head fails `upgrade head`, so the fork guard works.
- Coverage is measured (`--cov`) but not enforced: there is no `fail-under`, and
  the standards' "≥ 90 % for core modules" exists nowhere as a gate.
  **Recommendation:** add `fail_under` (repo-wide) or a per-module check if the
  gate is meant to be binding.
- The fix PRs add regression tests for every behavioral change (watermark hold,
  savepoint isolation, notify-after-commit, onsite-only suppression, retry
  exhaustion, robots 403, per-URL robots, captcha structural check, cooldown
  healthcheck, score clamp, channel guard, log level, enrichment gate and
  fetcher hardening).

## Operations

Reviewed: Dockerfile (multi-stage, non-root, `exec` entrypoint so SIGTERM reaches
the daemon), compose files (healthchecks, `depends_on: service_healthy`,
migrations on start), deploy workflow (concurrency-guarded, env rendered to temp
file and shipped over stdin, GHCR logout guard). No defects found beyond A3
(healthcheck vs. cooldown, fixed). `.env.example` is in sync with `Settings`.
