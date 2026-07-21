# Feature 6: LLM matching

**From build-plan:** feature 6
**Status:** done (2026-07-21)

## Goal

Stage 3: an LLM match of a fresh listing against the profile, returning a
structured, validated verdict, with resilient retry/fallback and full evaluation
metadata.

## Outcome

- `evaluation/schemas.py`: `MatchVerdict` (Pydantic) with `verdict`, `score`,
  `reasons`, `matching_skills`, `missing_requirements`, `risk_flags`. All fields
  required, no range/default constraints, so it round-trips through OpenAI strict
  structured outputs. `llm_error_fallback()` builds the safe no_match.
- `evaluation/prompts/match.v1.md`: the versioned system prompt (conservative
  screening rules, German-listing aware).
- `evaluation/llm.py`: `StructuredLlmClient` Protocol; `LlmMatcher.evaluate` does
  one call plus one retry on an invalid parse, then falls back to `no_match` with
  reason `llm_error` (the pipeline never breaks on the LLM). `LlmEvaluation`
  carries model, prompt_version, tokens, latency and a `reason()` JSON.
  `render_listing`, `load_prompt`, `is_match_notifiable(verdict, threshold)`, and a
  thin `OpenAiStructuredClient` adapter over `chat.completions.parse` (network,
  `# pragma: no cover`, mocked in tests via the Protocol).

## Build steps

- [x] **Step 1 - MatchVerdict schema + prompt**
- [x] **Step 2 - Matcher (retry/fallback), render, threshold, adapter**

## Tests

`test_llm.py`: match, no_match, retry-then-success, persistent-schema-violation
fallback, exception-then-success, persistent-exception fallback, threshold gate,
render, prompt load + missing. 94 tests total, full gate green; `llm.py` 98%.

## Design notes

- The OpenAI SDK is isolated behind `StructuredLlmClient`; business logic depends
  on the interface, so no live call is needed to test the retry/fallback logic.
- Model name comes from config (never hardcoded); tokens and latency are recorded
  per call for the reporting basis.
