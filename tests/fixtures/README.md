# Test fixtures

Saved payloads for tests. **No test makes a live request** (see
`blueprint/context/coding-standards.md`).

## freelancermap HTML (react-on-rails JSON)

The real site renders project data server-side into embedded
`<script class="js-react-on-rails-component" data-component-name="...">` JSON
blobs (`ProjectSearch` on list pages, `ProjectShow` on detail pages). The parser
reads those blobs, so these fixtures mirror that real structure.

- `freelancermap_list.html` — a `ProjectSearch` blob with three `initialResults`
  (id, slug, title, minute-precise `created`).
- `freelancermap_detail_asap_remote.html` — a `ProjectShow` blob for project 12345
  (start "ab sofort" → `startText`, 100 % remote → `remoteInPercent: 100`,
  minute-precise `created`, skills, HTML description).
- `freelancermap_detail_dated_onsite.html` — `ProjectShow` for project 67890
  (dated start `startYear/startMonth` = 2026-09, onsite → `remoteInPercent: 0`).

These mirror **real pages captured live on 2026-07-23**, trimmed to the consumed
field subset and sanitized (no real personal or company data). Slugs are shared
between the list and detail fixtures so the pipeline integration test can route
list → detail.

The parser raises `SelectorMismatchError` (never silent) when a blob is absent or
malformed, so a source-structure change is loud.

Edge cases these fixtures intentionally cover:

- URL from slug: `/projekt/{slug}` canonicalization.
- Start: "ab sofort" (`start_asap`) vs structured `startYear/startMonth`.
- Remote heuristic from percent: 100 → remote, 0 → onsite.
- posted_at precision: minute (ISO `created` with timezone).
- Lossless `raw`: the full `project` record is preserved on the listing.
