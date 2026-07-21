# Feature 11: Docker & home-server deployment

**From build-plan:** feature 11
**Status:** done (2026-07-21)

## Goal

Package the worker for the home server: a multi-stage image, a production compose
with postgres and a volume, a meaningful healthcheck, and migrations on start.

## Outcome

- `Dockerfile`: multi-stage on `python:3.13-slim`, dependencies installed with
  `uv` (from the official uv image), final stage runs as the non-root `pilot`
  user. Entrypoint applies migrations then execs the CLI (default `daemon`).
- `docker/entrypoint.sh`: `init-db` then `exec project-pilot "$@"`.
- `compose.yaml`: `app` + `postgres:16` + a `pgdata` volume. Postgres has a
  `pg_isready` healthcheck; the app waits for it and sets `DATABASE_URL` to reach
  the service. The app healthcheck runs `project-pilot healthcheck`.
- `healthcheck` CLI command + `ReportingService.is_healthy`: exits 0 only if the
  last successful/partial run finished within `3 x SCAN_INTERVAL_MIN`. The daemon
  now performs one scan at startup so the healthcheck has a baseline.
- `.dockerignore` and `docs/operations.md` (build, run, healthcheck, tuning,
  troubleshooting, migrations).

## Build steps

- [x] **Step 1 - Dockerfile + entrypoint + .dockerignore**
- [x] **Step 2 - compose.yaml + healthcheck command**
- [x] **Step 3 - operations doc**

## Verification and NIK-TODO

- `compose.yaml` and `compose.dev.yaml` validate with `docker compose config`.
- `docker build` **could not be run in the build sandbox**: the organizational
  egress policy blocks the Docker registry CDN (403 on the base-image pull), the
  same limit as Feature 1. The Dockerfile parses; the image must be built where
  Docker Hub and ghcr are reachable (a Nik first-run task).
- `is_healthy` is unit-tested (recent, missing, and stale run cases). 132 tests,
  full gate green.

## Design notes

- The profile is baked into the image (SPEC model: edit, rebuild, restart); the
  ops doc notes a volume-mount alternative for editing without a rebuild.
