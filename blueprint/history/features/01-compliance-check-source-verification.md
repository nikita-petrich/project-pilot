# Feature 1: Compliance check & source verification

**From build-plan:** feature 1
**Status:** done (2026-07-21)

## Goal

Establish the go/no-go for scraping freelancermap.de and pin down the facts
ingestion needs (initial-HTML vs JS, posted-date granularity), saving real pages
as fixtures. The only feature allowed live network access.

## Outcome

Live verification could not run in the build sandbox: outbound access to
`www.freelancermap.de` is blocked by the organizational egress policy (proxy 403
on CONNECT), and the proxy rules forbid routing around a policy denial. This is an
infrastructure limit, not a project STOP condition, so the build continued with an
adapted deliverable (per ADR 0001):

- `docs/compliance.md` records the finding, the binding runtime guardrails
  (robots.txt gate incl. Crawl-delay, identifying user agent, 2-5 s delays, the
  four STOP conditions), and the Nik-side live-snapshot procedure.
- `docs/adr/0001-source-verification.md` captures the decision and its
  consequences.
- Synthetic, clearly-labeled fixtures were created for parser development
  (`tests/fixtures/`), modeling the documented German structure with the SPEC's
  edge cases (ab sofort, keine Angabe, remote vs onsite, minute vs day posted
  precision, URL canonicalization inputs).

Compliance is enforced at runtime in code (Feature 4), so it does not depend on a
build-time check. The real live snapshot and selector/granularity confirmation are
a documented first-run task for Nik.

## Build steps

- [x] **Step 1 - Compliance finding + runtime requirements** - `docs/compliance.md` and `docs/adr/0001-source-verification.md` written.
- [x] **Step 2 - Synthetic fixtures** - list + 2 detail fixtures + fixtures README; all parse and cover the edge cases.

## Files

- `docs/compliance.md`, `docs/adr/0001-source-verification.md`
- `tests/fixtures/freelancermap_list.html`
- `tests/fixtures/freelancermap_detail_asap_remote.html`
- `tests/fixtures/freelancermap_detail_dated_onsite.html`
- `tests/fixtures/README.md`

## NIK-TODO carried forward

Before the first real run, on a networked machine: fetch and read robots.txt and
the ToS (STOP if the board is disallowed or automated access is forbidden), save
real pages over the synthetic fixtures, and confirm initial-HTML-vs-JS and the
posted-date granularity, adjusting the centralized selectors if needed.
