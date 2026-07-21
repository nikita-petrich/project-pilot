# ADR 0001: Source verification under a blocked build network

- Status: accepted
- Date: 2026-07-21
- Feature: 1 (compliance check & source verification)

## Context

Feature 1 is the only feature permitted live access to freelancermap.de. The plan
was to snapshot robots.txt and the ToS, then save one list page and two or three
detail pages as fixtures, and from those confirm two facts the ingestion code
needs: whether the project list is server-rendered in the initial HTML, and the
time granularity of the posted date.

The build environment cannot reach the site. The organizational egress proxy
answered 403 to the CONNECT for `www.freelancermap.de:443`, and the proxy
documentation forbids retrying or routing around a policy denial. No robots.txt,
ToS, or captcha could be evaluated, so none of the project STOP conditions applies;
the blocker is purely the sandbox network.

## Decision

Treat the blocked network like the build order treats missing infrastructure
(for example "Docker missing"): do not halt the build, do not circumvent the
policy, and split the feature's value into what can be done here versus what only
Nik can do on a networked machine.

1. **Enforce compliance in code, not by a one-time manual check.** The runtime
   robots.txt gate (`urllib.robotparser`, incl. Crawl-delay), the identifying user
   agent, the 2 to 5 second delays, and the 403/captcha cooldown are the real
   guarantee. They run every time the worker starts and scrapes, on Nik's network,
   independent of anything verified at build time.
2. **Develop against synthetic, clearly-labeled fixtures.** The Feature 4 parser
   and its tests use hand-built HTML in `tests/fixtures/` that models the
   documented German structure and edge cases. Selectors are centralized in one
   constants block so swapping in real HTML is a single-file edit.
3. **Tolerate unknown posted-date granularity by design.** `posted_at_precision`
   is `minute | day | unknown`, and the freshness gate falls back to the gap rule
   (distance to the last successful run) whenever precision is not minute. The
   pipeline is therefore correct without knowing the live granularity.
4. **Defer the live snapshot to Nik** as a documented first-run procedure in
   `docs/compliance.md`, including the go/no-go checks and the two technical
   questions to confirm.

## Consequences

- The build proceeds through all downstream features on synthetic fixtures.
- Real selectors and the JS-vs-initial-HTML question are unconfirmed until Nik runs
  the live snapshot; the centralized selectors and the `SelectorMismatchError`
  (parser raises rather than silently returning empty) make a mismatch loud and
  cheap to fix.
- Compliance does not depend on trust in a build-time check: if a board path is
  disallowed or a 403/captcha appears at runtime, the code aborts or cools down.
- If the live snapshot later shows the list is JS-only, ingestion must add a
  headless browser (Playwright), which the SPEC permits only once proven. That
  would be a new, scoped change, not a silent addition.

## Alternatives considered

- **Route around the egress policy** (a different fetch path or proxy): rejected.
  The proxy docs forbid it, and it conflicts with the strict no-circumvention
  compliance posture.
- **Halt the whole build and ask Nik:** rejected. The blocker is not a STOP
  condition and not ambiguous; a clear, documented fallback exists, and halting
  would stall eleven downstream features that only need fixtures.
