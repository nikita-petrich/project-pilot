# Feature 2: Config & profile foundation

**From build-plan:** feature 2
**Status:** done (2026-07-21)

## Goal

Boot-time foundation: one pydantic-settings model, a validated profile loader with
a stable `profile_hash`, and the shared domain errors plus `assert_defined`.

## Outcome

- `errors.py`: `ProjectPilotError` base with `ConfigError`, `SourceBlockedError`,
  `SelectorMismatchError`, `LlmSchemaError`, and a PEP-695 `assert_defined[T]`.
- `config.py`: `Settings(BaseSettings)` mapping the `.env.example` variables.
  Always-on invariants: `SCAN_INTERVAL_MIN >= 15`, `ANALYSIS_WINDOW_MIN >= 1`,
  `MATCH_THRESHOLD in 0..100`, known `LOG_LEVEL`. `SEARCH_URLS` parses from CSV via
  a `NoDecode` before-validator. `user_agent()` builds the compliance UA;
  `require_search_urls/telegram/openai` fail fast with `ConfigError` only when a
  command needs them (so the app stays importable without a full `.env`, and no
  secret is invented). `load_settings()` is the boot entry.
- `profile_loader.py`: `ProfileConstraints` (Pydantic), a frozen `Profile` value
  object, and `ProfileService.load()` computing the SHA-256 hash of the two raw
  files; missing files, non-mapping YAML, and schema violations raise `ConfigError`.

## Build steps

- [x] **Step 1 - Errors + assert_defined**
- [x] **Step 2 - Settings**
- [x] **Step 3 - ProfileService**

## Tests

`test_errors.py`, `test_config.py`, `test_profile_loader.py` (27 tests total).
Coverage: errors 100%, profile_loader 96%, config 94%. Full gate green.

## Design notes

- Fail fast at CLI boot, not at import. Secrets default to empty and are enforced
  only where used, matching the build order's "run a smoke test if the secret is
  set" flexibility.
- `profile_hash` is the SHA-256 of the raw `profile.md` + `constraints.yaml`
  bytes, so any edit produces a new version marker (as the SPEC intends).
