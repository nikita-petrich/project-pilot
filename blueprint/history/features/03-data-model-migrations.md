# Feature 3: Data model & migrations

**From build-plan:** feature 3
**Status:** done (2026-07-21)

## Goal

The persistence layer: typed SQLAlchemy 2.0 entities per SPEC section 4, an async
Alembic setup with the initial migration, and the repository methods the pipeline
needs.

## Outcome

- `models.py`: `Base` + `Listing`, `Evaluation` (1:n to listing), `Run`,
  `SourceState`, all `Mapped[...]` typed. Six native PG enums via a `_pg_enum`
  helper whose labels are the members' lowercase string values. JSONB for
  `skills`, `raw`, and `reason`; all timestamps `timestamptz`; tz-aware UTC
  defaults.
- Alembic async template wired to `Base.metadata` and `DATABASE_URL` (resolved via
  `Settings`, escaped for configparser), `compare_type=True`. The `script.py.mako`
  was modernized to `str | Sequence[str] | None` so generated migrations pass
  strict ruff. Initial migration `3c057a6c1e39` creates all four tables + indexes;
  `downgrade()` also drops the enum types, so up/down/up round-trips (verified).
- `db.py`: async engine, session factory, and a `session_scope` unit-of-work
  context manager (commit on success, rollback on error).
- `repository.py`: `Repository` over one session with `count_listings`,
  `get_known_hashes`, `get_listing_by_hash`, `upsert_listing` (insert-or-touch,
  stage 0), `add_evaluation`, `start_run`/`finalize_run`, and
  source-state/watermark helpers.

## Build steps

- [x] **Step 1 - Entities + enums** (`models.py`)
- [x] **Step 2 - Alembic async setup + initial migration** (applied and round-tripped)
- [x] **Step 3 - db.py + repository methods**

## Tests

`tests/conftest.py` provides a Postgres-backed `session` fixture that builds the
schema with `create_all` and skips cleanly when no DB is reachable.
`test_repository.py` and `test_db.py` (36 tests total, run against real Postgres
here). Coverage: models 100%, db 100%, repository 99%. Full gate green.

## Design notes

- `create_all` is used only in tests; production schema changes go through Alembic.
- Enum labels are stored as the StrEnum values (stable lowercase identifiers used
  consistently across DB and code), via `values_callable`.
