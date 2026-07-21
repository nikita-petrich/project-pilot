# Feature 9: Resilience & self-monitoring

**From build-plan:** feature 9
**Status:** done (2026-07-21)

## Goal

Make the worker robust and self-reporting: retry transient failures, back off hard
on blocking, and warn on repeated failures.

## Outcome

- `ingestion/client.py`: `PolitenessClient.get` now retries transient failures
  (network errors, 5xx, 429) with exponential backoff and jitter via tenacity,
  using an injectable wait so tests are instant. A 403 or captcha is never retried
  and raises `SourceBlockedError` immediately. Retries exhausted on a 5xx/429
  return the last response (the caller treats it as a fetch failure).
- `pipeline.py`: a cooldown gate skips the run while `source_state.cooldown_until`
  is in the future. On a `SourceBlockedError` the run sets a 6-hour cooldown and
  sends a one-time Telegram warning (one-time because the next run is skipped). Any
  error run increments `consecutive_failures`; a success or partial run resets it
  and clears cooldown. A single warning fires when failures reach three.

## Build steps

- [x] **Step 1 - tenacity retry (never on 403)**
- [x] **Step 2 - cooldown + failure warnings**

## Tests

`test_client.py` gained retry tests (5xx-then-success, transport-error-then-success,
403-not-retried, retries-exhausted-returns-last). `test_pipeline.py` gained cooldown
set + warn, cooldown skips next run, and three-consecutive-failures-warn-once. 123
tests, full gate green; client 95%, pipeline 93%.

## Design notes

- Cooldown/failure state lives on the existing `source_state` row and is mutated
  in-session, so it commits with the run's unit of work.
- The one-time semantics are structural: entering cooldown suppresses the next
  run's block, and a success resets the failure streak.
