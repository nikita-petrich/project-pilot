# Feature 4: Scraper ingestion

**From build-plan:** feature 4
**Status:** done (2026-07-21)

## Goal

Turn freelancermap pages into normalized `ParsedListing` objects, politely and
compliantly, developed and tested only against the synthetic fixtures.

## Outcome

- `ingestion/normalize.py`: `canonicalize_url` (drop query/fragment/trailing
  slash, lowercase host), `compute_url_hash` (sha256), German date parsing,
  `parse_start` (ab sofort -> asap, keine Angabe -> neither), `parse_end`,
  `remote_status_from_text` (hybrid/onsite/remote heuristic with a word-boundary
  "nein"), and `parse_posted` (minute precision from `<time datetime>`, else day
  precision from a German date via Europe/Berlin midnight, else unknown).
- `ingestion/parser.py`: selectors in one constants block; `ListingSummary` and
  `ParsedListing`; `parse_list_page` and `parse_detail_page` (facts read by German
  label); raises `SelectorMismatchError` instead of returning empty.
- `ingestion/watermark.py`: `evaluate_page` stop criterion (stop on a known hash
  or an older-than-watermark listing; seed run processes all).
- `ingestion/client.py`: `PolitenessClient` with an identifying UA header, an
  async robots.txt gate (`urllib.robotparser`, Crawl-delay honored), an injectable
  sleeper for the 2-5 s spacing, a request timeout, and 403/captcha ->
  `SourceBlockedError`, robots-disallow -> `ConfigError`.

## Build steps

- [x] **Step 1 - Normalization**
- [x] **Step 2 - Parser**
- [x] **Step 3 - Watermark**
- [x] **Step 4 - Politeness client**

## Tests

`test_normalize.py`, `test_parser.py`, `test_watermark.py`, `test_client.py`
(respx-mocked, no live requests). 69 tests total, full gate green. Coverage:
watermark 100%, normalize 94%, client 94%, parser 92% (all >= the 90% target).

## Design notes

- Compliance is anchored in code: the robots gate aborts at startup on a
  disallowed path, and the client never disguises itself or retries a 403.
- Blocking calls avoided: robots.txt is fetched with httpx and fed to
  `RobotFileParser.parse`; delays use an async sleeper injected for fast tests.
- Selectors are unverified against the live site (fixtures are synthetic); the
  single constants block plus `SelectorMismatchError` make a real-markup swap cheap.
