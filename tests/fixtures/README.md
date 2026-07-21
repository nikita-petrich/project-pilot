# Test fixtures

Saved payloads for tests. **No test makes a live request** (see
`blueprint/context/coding-standards.md`).

## freelancermap HTML (synthetic)

- `freelancermap_list.html` - a search-results page with four project cards.
- `freelancermap_detail_asap_remote.html` - detail for project 12345 (start "ab
  sofort", 100% remote, minute-precise posted `<time>`).
- `freelancermap_detail_dated_onsite.html` - detail for project 67890 (start
  01.09.2026, end "keine Angabe", onsite, date-only posted).

These three are **synthetic**: they were hand-built because the build sandbox
could not reach freelancermap.de (`docs/compliance.md`, `docs/adr/0001`). They
model the documented German structure and the edge cases the parser must handle,
but they are not guaranteed to match the site's current markup.

**Before production**, Nik replaces them with real saved pages (keep the file
names) and, if needed, adjusts the centralized selectors in
`src/project_pilot/ingestion/parser.py`. The parser raises `SelectorMismatchError`
rather than silently returning empty data, so a markup mismatch is loud.

Edge cases these fixtures intentionally cover:

- URL canonicalization: relative vs absolute hrefs, tracking query params, a
  fragment, and a trailing slash.
- German dates: "ab sofort" (start-asap flag), "01.09.2026", "keine Angabe".
- Remote heuristic: "100 % Remote", "Nein, vor Ort", "hybrid".
- posted_at precision: a machine-readable `<time datetime>` (minute) vs a bare
  German date (day).
