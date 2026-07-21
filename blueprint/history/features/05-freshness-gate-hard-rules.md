# Feature 5: Freshness gate & hard rules

**From build-plan:** feature 5
**Status:** done (2026-07-21)

## Goal

The two deterministic (0-token) evaluation stages: freshness gating and the
hard-rule engine over `constraints.yaml`.

## Outcome

- `evaluation/freshness.py`: `evaluate_freshness(...)` -> `FreshnessResult`.
  Signal order: minute-precise `posted_at` first (fresh if `now - posted_at <=
  window`), else the gap rule against the watermark (fresh only if the last
  successful run is within the window, so a post-downtime backlog is persisted but
  not analysed). No watermark (seed/first run) is not fresh. Reasons are JSON with
  the signal, the gap in minutes, and the window.
- `evaluation/rules.py`: `apply_hard_rules(text, constraints)` -> `RuleResult`.
  Blacklist (first hit wins, reason `{rule, matched_term}`) then must_have (reason
  `{rule, required}`). Matching uses a `[a-z0-9]` lookaround boundary so `c#`,
  `c++`, `.net` match as whole tokens and `java` does not match inside
  `javascript`; case-insensitive.

## Build steps

- [x] **Step 1 - Freshness gate**
- [x] **Step 2 - Hard-rule engine**

## Tests

`test_freshness.py` (minute fresh/stale, gap fresh/stale, day-precision uses gap,
no watermark) and `test_rules.py` (blacklist, case-insensitivity, java/javascript,
c#/c++/.net, must_have satisfied/missing, precedence). 83 tests total, full gate
green; both modules at 100% coverage.

## Design notes

- Results are frozen dataclasses with plain-dict reasons (stored later as JSONB);
  Pydantic is reserved for external boundaries per the coding standards.
- Language filtering (`constraints.languages`) is intentionally deferred: it would
  need a language detector; the field stays available for the LLM prompt/config.
