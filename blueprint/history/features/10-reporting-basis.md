# Feature 10: Reporting basis

**From build-plan:** feature 10
**Status:** done (2026-07-21)

## Goal

A read-only reporting layer over the stored evaluations, surfaced through the
`stats` CLI command.

## Outcome

- `reporting.py`: `ReportingService` with `total_listings`, `listings_by_status`,
  `verdict_distribution`, `matches_per_day(days)`, `top_no_match_terms(limit)`
  (aggregates the hard-rule `matched_term` from the JSONB reason), and
  `token_usage(days)` (LLM call count and token sums). `build_report` assembles a
  `Report`; `format_report` renders a compact plain-text summary. An injectable
  `now` keeps the time-windowed queries testable.
- `cli.py`: the `stats` command prints the report (needs only `DATABASE_URL`).

## Build steps

- [x] **Step 1 - Reporting queries + Report/format**
- [x] **Step 2 - stats CLI command**

## Tests

`test_reporting.py`: verdict distribution, listings-by-status, matches-per-day,
top no-match terms, token usage, and build+format. 129 tests, full gate green;
reporting 100%. `stats` verified against an empty DB (clean zeros).

## Design notes

- Queries use SQL aggregation (group by, JSONB path extraction) rather than
  loading rows into Python.
- No `Any`: results are unpacked from typed selects; the JSONB term extraction
  uses `func.jsonb_extract_path_text`.
