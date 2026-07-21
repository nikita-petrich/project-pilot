# Build Plan

> Checkbox list in build order. `/feature` specs the next unchecked item.
> Scaffolding (uv init, dependency setup) and blueprint installation are
> pre-build steps and deliberately not listed here.
> Detailed domain rules: `SPEC.md` in the repo root (binding, esp. §3–5).

## MVP

- [x] 1. **Compliance check & source verification** - snapshot robots.txt and ToS (`docs/compliance.md`), document go/no-go; check whether the project list is in the initial HTML or needs JS rendering and what time granularity `posted_at` has (minute-precise vs. date-only); record the result as an ADR — halt on a STOP condition and ask Nik
- [x] 2. **Config & profile foundation** - pydantic-settings config (SCAN_INTERVAL_MIN hard ≥ 15, startup abort on violation), `.env.example`, ProfileService loads `profile/profile.md` + `profile/constraints.yaml` (Pydantic-validated) at boot and computes `profile_hash`; `errors.py` with error classes + `assert_defined`
- [x] 3. **Data model & migrations** - SQLAlchemy 2.0 entities `listings`, `evaluations`, `runs`, `source_state` per SPEC §4 (typed, native PG enums, JSONB), Alembic async setup + initial migration, repository methods (get_known_hashes, upsert_listing, record_run)
- [x] 4. **Scraper ingestion** - politeness client on httpx.AsyncClient (user agent, robotparser gate, 2–5 s delay, timeouts), list-page parser with BeautifulSoup/lxml (selectors centralized as a constants block), detail fetch only for new hashes, watermark pagination with stop criterion, normalization (URL canonicalization, German date formats, remote heuristic) — tests exclusively against saved HTML fixtures
- [x] 5. **Freshness gate & hard rules** - analysis-window logic (ANALYSIS_WINDOW_MIN, posted_at precision or gap rule as fallback), `skipped_stale` with reason JSON; deterministic rule engine from constraints.yaml with reason `{rule, matched_term}` — word-boundary matching tested incl. special cases c#, c++, .net
- [x] 6. **LLM matching** - prompt v1 as a file (`evaluation/prompts/match.v1.md`), OpenAI `.parse()` against Pydantic model `MatchVerdict` (verdict, score, reasons, matching_skills, missing_requirements, risk_flags), evaluation persistence with model/prompt_version/profile_hash/tokens/latency, MATCH_THRESHOLD decision, 1 retry on schema violation, fallback verdict `no_match` with reason `llm_error` on failure
- [x] 7. **Telegram notification** - lean httpx client against the Bot API, HTML message format (title link, score, top reasons, start, location/remote), digest for multiple matches per run, `notified_at` only after a successful send (otherwise retry in the next run), `test-notify` CLI command
- [ ] 8. **Scheduler & pipeline runner** - AsyncIOScheduler with 15-min interval, jitter, `max_instances=1`/`coalesce=True`, seed-run detection (empty DB ⇒ persist, evaluate/send nothing), orchestration stages 0–3, a failing single entry only skips that entry, `runs` protocol per run, clean SIGTERM handling, `run-once` stays cron-compatible (non-zero exit code on a failed run)
- [ ] 9. **Resilience & self-monitoring** - tenacity retry with exponential backoff + jitter for network errors/5xx/429 (never for 403), 403/captcha detection ⇒ `SourceBlockedError` ⇒ 6-h cooldown in `source_state` + one-time Telegram warning, warning after 3 consecutive failed runs (with a flag against warning spam)
- [ ] 10. **Reporting basis** - query service or SQL views: verdict distribution, matches/day, top no-match reasons, LLM token costs per period; output via the `stats` CLI command
- [ ] 11. **Docker & home-server deployment** - multi-stage Dockerfile (python:3.13-slim, uv, non-root), `compose.yaml` with app + postgres + volume, healthcheck (last successful run < 3 × interval), Alembic migrations on start, operations doc
- [ ] 12. **README & legal** - setup (uv sync, .env, profile), commands, operation, threshold tuning, troubleshooting (cooldown, selector breakage); section on the robots.txt/ToS finding, rate limits, user agent, personal use only — references `docs/compliance.md`

## Post-MVP

- [ ] 13. **Telegram commands** - `/stats`, `/pause`, `/resume`, `/last` directly in the bot (then switch to aiogram)
- [ ] 14. **Re-evaluation command** - re-evaluate historical listings with a new profile hash (uses the 1:n `evaluations` structure), comparison report old vs. new
- [ ] 15. **Queue-based multi-source workers** - decouple fetcher ↔ pipeline via a Redis queue (`arq`), one fetcher per source as a stateless worker behind a JSON message contract; preparation for further platforms (SPEC §8)
