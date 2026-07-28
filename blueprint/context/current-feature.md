# Current Feature

> **Generated file.** Holds the one feature or fix being built right now. Run
> `/feature <number-or-name>` to spec a build-plan feature, or `/fix "<bug>"` for
> an ad-hoc fix. Build one thing at a time; `/complete` archives it (to
> `blueprint/history/features/` or `blueprint/history/fixes/`) and resets this file.

## Feature 18: Kontakt-Anreicherung (contact enrichment: company website + LinkedIn/Google discovery)

**Goal.** Given a match's company (and, when known, the Ansprechpartner), find the
real contact channel — e-mail and phone — and enrich our data with it. The
automated, working source is the **company's own website**: discover the site via a
pluggable web search, then read its **Impressum / Kontakt / Team / Karriere** pages
(the legally-mandated German contact data) and extract e-mails, phone numbers, and
contact-person names. LinkedIn and Google are surfaced as **one-click research
links** (company search + people search), never scraped. Results are shown in Slack
(a "🔎 Find contact" button on the match and on a recipient-less draft) and stored
as `contact_leads` for traceability; the found e-mail can become the application
recipient.

**Why links, not scraping, for LinkedIn/Google (binding design decision).**
- LinkedIn's User Agreement forbids automated scraping; it is also technically
  infeasible here (auth wall, anti-bot) and never exposes phone/e-mail publicly. A
  scraper would be both non-compliant and useless for the stated goal.
- This project is compliance-first (robots gate, never bypass 403/captcha). Reading
  a company's public Impressum is the sanctioned, effective source of phone/e-mail.
- So LinkedIn/Google are honored as deep links Nik opens in his own browser session;
  the crawler only fetches company websites, politely and robots-aware.

**Out of scope.** No LinkedIn/Google HTML scraping. No paid data brokers. No bulk
crawling — bounded page budget per company. No new Slack slash command (the button
needs no Slack-app change). Feature stays **opt-in** (`ENRICHMENT_ENABLED`), so it
never makes surprise outbound calls.

### Steps

- [x] 1. Config & schemas: `ENRICHMENT_ENABLED`, `ENRICHMENT_SEARCH` (`duckduckgo|none`),
  `ENRICHMENT_MAX_PAGES` in `Settings` + `.env.example`; `enrichment/schemas.py`
  (`SearchResult`, `DiscoveryLinks`, `ContactEnrichment`).
- [x] 2. Pure extractors `enrichment/extract.py`: e-mail extraction (dedupe,
  de-obfuscate `[at]`/`[dot]`, drop noreply/asset false positives), German/intl phone
  extraction, contact-person names, and contact-page link discovery — full unit tests.
- [x] 3. Discovery links `enrichment/links.py`: LinkedIn company & people search URLs,
  Google search URLs — pure builders + tests.
- [x] 4. Web access: `enrichment/fetch.py` `WebFetcher` (identifying UA, timeout,
  polite delay, best-effort robots, never-retry-403) behind a `Fetcher` protocol;
  `enrichment/search.py` `SearchProvider` protocol + `DuckDuckGoSearch` (pure result
  parser) + `NullSearchProvider` — respx/fixture tests, no live HTTP.
- [x] 5. `EnrichmentService` (`enrichment/service.py`): find website → fetch homepage +
  Impressum/Kontakt/Team/Karriere (bounded) → extract → rank e-mails (person-match,
  then role, then rest) → build links → `ContactEnrichment`; errors never crash the
  caller. Unit tests with fake fetcher + fake search.
- [x] 6. Persistence & listing wiring: `ContactLead` model + Alembic migration + repo
  methods; `ListingEnrichmentService.enrich_listing(listing_id)` derives
  company/person/known-email from the stored listing and persists the lead.
- [x] 7. CLI: `project-pilot enrich "<company>" [--person] [--url]` and
  `enrich --listing-id N`; prints e-mails, phones, persons, website, and the links.
- [x] 8. Slack: `format_contact_blocks`; "🔎 Find contact" button on match messages
  and on recipient-less drafts; bot routes the `enrich` action via an
  `EnrichmentFlow`; wire into `_build_bot`/daemon. Unit tests (fake flow + poster).
- [x] 9. Docs: README enrichment section + compliance note, AGENTS.md command,
  build-plan entry 18.

### Follow-up (requested)

- [ ] 10. LinkedIn **Vernetzungsnachricht**: `enrichment/message.py` pure builder
  (personalized from person/company/title, ≤300 chars, optional `APPLICANT_NAME`
  signature); add `linkedin_message` to `ContactEnrichment` + `ContactLead`,
  populate in `EnrichmentService`, render as a copyable block in Slack, print in the
  CLI. Guaranteed present so Nik can send the connection request himself. Unit tests.
- [ ] 11. Optional **rendered fetching** for JS-heavy sites: `enrichment/robots.py`
  shared `RobotsGate` (reused by both fetchers); `enrichment/render.py`
  `PlaywrightFetcher` (headless Chromium, identifying UA, timeout, polite delay,
  robots-aware, never-retry-403, lazy import). Config `ENRICHMENT_RENDER` (default
  off) + optional `ENRICHMENT_RENDER_BROWSER_PATH`; `playwright` as an **optional**
  extra so the default install/Docker/CI stay unchanged. Wire selection in the CLI.
  Tests for the robots gate + fetcher selection (browser calls stay a covered-out
  boundary, like Socket Mode).
- [ ] 12. "Gerne per Du": `OUTREACH_OFFER_DU` (default on) makes the connection
  message offer the recipient first-name terms ("Gerne auch per Du."), threaded
  through `EnrichmentService` → `build_connection_message`. Unit + config tests.

### Done when

- With `ENRICHMENT_ENABLED=true`, `project-pilot enrich "<company>"` prints found
  e-mails, phones, contact persons, the company website, and LinkedIn/Google links.
- On a match's Slack message, "🔎 Find contact" posts those results in the thread;
  a recipient-less draft offers the same button, and a found e-mail can be set as the
  recipient by replying with it.
- No LinkedIn or Google page is ever fetched; only company websites are crawled, and
  robots.txt is respected (disallowed pages skipped, 403 never retried).
- `contact_leads` records each lookup for traceability.
- Every enrichment result carries a ready-to-copy LinkedIn connection message
  (shown in Slack and printed by the CLI), so Nik can reach the person directly.
- With `ENRICHMENT_RENDER=true`, contact pages are fetched with a headless browser
  so JS-rendered e-mail/phone data is found too; default stays the httpx fetcher.
- Quality gate green: `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`, `uv run pytest`.
