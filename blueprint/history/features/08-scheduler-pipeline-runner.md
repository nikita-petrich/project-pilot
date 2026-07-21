# Feature 8: Scheduler & pipeline runner

**From build-plan:** feature 8
**Status:** done (2026-07-21)

## Goal

Tie the stages together: a run-once orchestration over stages 0-3 with the runs
protocol, seed-run detection, per-entry isolation, and a scheduler daemon with
overlap protection and clean shutdown.

## Outcome

- `pipeline.py`: `Pipeline.run_once(now=None)` opens one session (unit of work),
  records a `runs` row, fetches list pages with watermark pagination, fetches
  detail only for new listings, and either seed-persists (empty DB -> all
  `skipped_stale`, no analysis or notifications) or evaluates each fresh entry
  through freshness -> hard rules -> LLM, then notifies. Per-entry failures are
  isolated (the run continues, status becomes `partial`); a `SourceBlockedError`
  aborts the run as `error`. Notifications go out as one digest; `notified_at` is
  set only after a successful send, and `get_unnotified_matches` retries prior
  failures. The pipeline depends on `SourceClient`/`Matcher`/`Notifier` Protocols,
  so it is fully testable with fakes.
- `scheduler.py`: `SchedulerRunner` wraps `AsyncIOScheduler` (interval, jitter,
  `max_instances=1`, `coalesce=True`), installs SIGTERM/SIGINT handlers that
  request a clean stop, and shuts down on stop.
- `cli.py`: `init-db` (alembic upgrade head), `run-once` (non-zero exit on a
  failed run), `daemon`, plus the pipeline builder. Repository gained
  `get_unnotified_matches` and `mark_notified`; parser gained `parse_next_page_url`.

## Build steps

- [x] **Step 1 - Pipeline orchestration + runs protocol + seed + isolation**
- [x] **Step 2 - Scheduler daemon (overlap protection, clean stop)**
- [x] **Step 3 - CLI wiring (init-db, run-once, daemon)**

## Tests

`test_pipeline.py` (seed, full-run-notifies, per-entry isolation, source-blocked
error, notification retry, dedupe, stale-skip, blacklist-before-LLM, dry-run) and
`test_scheduler.py` (job registration, clean stop). 116 tests, full gate green;
pipeline 90%. CLI wiring verified by running `--help`, `init-db`, and `run-once`.

## Design notes

- `run_once` takes an optional `now` for deterministic freshness in tests.
- CLI DB/network commands are integration wiring (ride on run evidence); the
  Pipeline logic carries the unit/integration tests.
