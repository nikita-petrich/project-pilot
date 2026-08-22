# Compliance and source verification (freelancermap.de)

Status: **live verification deferred to Nik** (see "Build-environment finding").
This document is the binding compliance record for project-pilot. The runtime
guardrails described here are enforced in code (Feature 4, `ingestion/client.py`)
and must never be weakened.

## Why we scrape at all

freelancermap.de offers no public read API for its project board, its RSS feed was
discontinued, and the official "Projektagent" only sends a digest email once per
day. A same-day email is too slow for the goal (react within minutes). A moderate,
robots-respecting, public-pages-only scraper is therefore the chosen approach.
Personal, single-user use only. No resale or redistribution of scraped data.

## Build-environment finding (why there is no live snapshot in this repo)

The live snapshot (robots.txt, ToS, one list page, two or three detail pages) was
**not** taken in the build environment. Outbound HTTPS to `www.freelancermap.de`
is blocked by this session's organizational egress policy:

```
GET https://www.freelancermap.de/robots.txt
-> proxy answered 403 to CONNECT (policy denial), host www.freelancermap.de:443
```

The agent-proxy documentation is explicit that a 403 on CONNECT is a policy denial
that must be reported, not retried or routed around. This is an infrastructure
limit of the build sandbox, **not** one of the project STOP conditions (which are
about what robots.txt / the ToS / a captcha say once the site is reachable).

Consequence: the parser and tests are developed against clearly-labeled
**synthetic** fixtures (`tests/fixtures/`), and the real live snapshot plus a
selector/granularity confirmation is a first-run task for Nik on his own network
(his home server, which is where the worker actually runs). The runtime robots.txt
gate enforces compliance there regardless of what the fixtures assumed. See
`docs/adr/0001-source-verification.md`.

## Binding runtime guardrails (enforced in code)

These come from `SPEC.md` section 5 and `blueprint/context/coding-standards.md`.

1. **Public pages only.** No login, no login bypass, no captcha or bot-protection
   circumvention, no user-agent or proxy rotation to disguise the client.
2. **robots.txt gate at startup.** `urllib.robotparser` fetches and parses
   `https://www.freelancermap.de/robots.txt`. If any configured search or detail
   path is disallowed for our user agent, the process **aborts at startup** with a
   clear message. The parser's `Crawl-delay` is read and honored (the effective
   inter-request delay is `max(configured_delay, crawl_delay)`).
3. **Identifying user agent.**
   `project-pilot/1.0 (personal project alert bot; contact: <CONTACT_MAIL>)`.
   The contact mail comes from the `CONTACT_MAIL` env var (`SPEC.md` section 5.4).
4. **Moderate rate.** Per run: at most 2 list pages per search URL (watermark
   pagination usually stops earlier), and detail pages only for **new** listings. A
   random 2 to 5 second delay separates requests; every request has a timeout.
5. **Back off on blocking.** An HTTP 403 or a captcha indicator aborts the run,
   sets `source_state.cooldown_until = now + 6h`, and opens a single operator
   warning session. No retry hammering; 403 is never retried (Feature 9).
6. **Secrets via env only.** No secrets in the repo; `.env.example` is maintained.

## STOP conditions (halt, document, ask Nik)

The agent halts the whole build and asks Nik if any of these is observed **while
the site is reachable**:

- robots.txt disallows the project board paths for crawlers.
- The Terms of Service explicitly prohibit automated access.
- A captcha or bot wall appears even on moderate single requests.
- Any ambiguity that would water down the guardrails above.

None of these was triggered in this build (the site was unreachable, so none could
be evaluated). They remain live checks Nik performs during the first real run, and
they are re-checked continuously at runtime by the robots.txt gate and the
403/captcha cooldown.

## Nik live-snapshot procedure (do this before the first real run)

On the machine where the worker will run (with network access to the site):

1. Fetch and read `https://www.freelancermap.de/robots.txt`. Confirm the project
   board paths you put in `SEARCH_URLS` are **not** disallowed for `*` or for a
   `project-pilot`/bot user agent, and note any `Crawl-delay`.
2. Read the current Terms of Service and confirm automated access is not expressly
   forbidden. If it is, that is a STOP: do not run the scraper.
3. With the user agent above and a 3 to 5 second pause between requests, save one
   list page and two or three detail pages as real fixtures, replacing the
   synthetic ones in `tests/fixtures/` (keep the file names).
4. Confirm the two open technical questions and, if needed, adjust the centralized
   selectors in `ingestion/parser.py` and the posted-date parsing:
   - **Initial HTML vs JS:** is the list of projects present in the raw HTML
     response (View Source), or only after JavaScript runs? If JS-only, ingestion
     needs a headless browser (Playwright); the SPEC allows this only if proven.
   - **posted_at granularity:** does the "Eingetragen" (posted) timestamp carry
     minute precision, only a calendar date, or a relative label ("vor 2 Stunden")?
     This decides whether freshness uses `posted_at` directly or the gap-rule
     fallback. The code already tolerates all three via `posted_at_precision`.
5. Re-run `uv run pytest` after swapping in real fixtures and adjusting selectors.

## Working assumptions until confirmed

These drive the synthetic fixtures and are flagged as unverified:

- The project list is server-rendered in the initial HTML (freelancermap has
  historically been server-rendered). Assumed until step 4 confirms.
- The posted date is shown as a German date, possibly with a relative or precise
  component. Treated as `day` precision by default, upgraded to `minute` only if a
  precise machine-readable timestamp (for example a `<time datetime="...">`) is
  present. The freshness gap-rule fallback makes the pipeline correct either way.
- German field conventions: start "ab sofort" (maps to a `start_asap` flag), dates
  like "01.09.2026", "keine Angabe" for unknown, "Remote"/"Homeoffice" for remote.
