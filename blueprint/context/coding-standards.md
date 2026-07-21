# Coding Standards

> Conventions for project-pilot (Python worker, no frontend). Replaces the
> blueprint's Next.js defaults — on conflict, this file rules.
> Secondary project goal: idiomatic, modern Python as a CV reference —
> readability and type strictness are product properties.

## Python & Typing

- Python 3.13, complete type annotations everywhere; `mypy --strict` is the gate before every `/check`
- No `Any` — `object`/generic TypeVars + narrowing; `cast()` only at system boundaries (parsed LLM output, raw data) and always directly behind a Pydantic validation
- Null safety via `assert_defined[T](value: T | None, msg: str) -> T` in `errors.py` (PEP-695 generic): raises a domain error with a meaningful message instead of scattered `# type: ignore` or bare `assert`
- Pydantic v2 models are the source of truth for config, constraints.yaml, and LLM verdicts; derive types from them, no parallel definitions
- Modern syntax: `X | None` instead of `Optional[X]`, `list[str]` instead of `List[str]`, `StrEnum` for domain enums

## Async & Architecture

- asyncio throughout: `httpx.AsyncClient`, async SQLAlchemy sessions, `AsyncIOScheduler`; blocking libraries (`requests`, `time.sleep`) are forbidden — `asyncio.sleep` for delays
- No framework magic: explicit composition — objects are constructed in `cli.py`/`main` and passed as constructor dependencies (in tests, simply pass fakes)
- One module per domain per the project structure in `SPEC.md` §6: `ingestion`, `evaluation`, `notification`, `pipeline`, `reporting`, plus `config`, `db`, `models`, `repository`, `profile_loader`, `errors`
- Layers: repository (data access only) → service (domain logic) → runner/scheduler (orchestration). Errors are not swallowed in the repository, are enriched with context in the service (`raise NewError(...) from err` — the cause chain is preserved), and are decided in the runner: skip the entry vs. abort the run
- Domain errors as own classes in `errors.py` (`SourceBlockedError`, `SelectorMismatchError`, `LlmSchemaError`) — never branch on message string matching

## SQLAlchemy & Database

- SQLAlchemy 2.0 typed style: `Mapped[...]` + `mapped_column`, DeclarativeBase; JSONB and native PG enums via the postgresql dialect types
- Alembic migrations only (async template) — never `create_all` outside of tests
- One session per pipeline run (unit of work), via async context manager; no session use across run boundaries
- All timestamps `timestamptz` in UTC (`datetime` always aware); display timezone Europe/Berlin only at output

## Configuration & Secrets

- One `pydantic-settings` model parses ENV at boot; validation errors ⇒ immediate process abort with a clear message (fail fast)
- Hard invariants in validation: `SCAN_INTERVAL_MIN >= 15`, `SEARCH_URLS` not empty, Telegram credentials present
- Secrets exclusively via ENV; `.env` in `.gitignore`, `.env.example` is updated in the same commit as any new variable

## Scraping Behavior (compliance anchored in code)

- robots.txt gate at startup (`urllib.robotparser`, incl. Crawl-delay); disallowed path ⇒ startup abort
- User agent from config (with contact mail), random 2–5 s delay between requests, timeout on every request
- CSS selectors centralized as a constants block at the top of `ingestion/parser.py` (one place to adapt on HTML changes); the parser raises `SelectorMismatchError` instead of silently returning empty data
- tenacity retry (exponential backoff + jitter, max. 3) only for network errors/5xx/429 — **never** for 403/captcha

## LLM Usage

- Prompts as versioned files under `src/project_pilot/evaluation/prompts/` (`match.v1.md`, …); `prompt_version` is persisted on every evaluation
- Outputs exclusively via OpenAI `.parse()` against the Pydantic model `MatchVerdict`; schema violation ⇒ one retry, then fallback verdict `no_match` with reason `llm_error` — the pipeline never breaks because of the LLM
- Model name from ENV, never hardcoded; log tokens and latency per call
- No personal data in the prompt other than the profile; the profile text counts as sensitive and is not logged

## Tests

- pytest + pytest-asyncio; unit tests for normalization, freshness gate, rule engine (incl. c#/c++/.net word-boundary cases), dedupe/upsert
- **No live HTTP requests in tests** — HTML/JSON/LLM responses as fixtures under `tests/fixtures/`; HTTP is mocked with `respx`
- Integration test: full `run_once` with a fixture source, local Postgres (compose.dev.yaml or Testcontainers), and mocked Telegram/LLM
- Core modules (`evaluation`, `ingestion/normalize`, dedupe) ≥ 90% coverage; `pytest --cov` gate in the check script

## Style, Naming, Workflow

- ruff for lint **and** format (strict rule set incl. import sorting); `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest` run on every `/check`
- Naming: modules/functions/variables snake_case, classes PascalCase, constants SCREAMING_SNAKE_CASE; files small and focused, one purpose per module
- Docstrings (Google style) for all public functions/classes — brief, what and why, not how
- Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`); small, thematically closed commits per implement step
- Logging via stdlib `logging` with module loggers and a run ID in context; no `print` in production code
- No dead code, no commented-out blocks in commits; TODOs only with a feature reference
