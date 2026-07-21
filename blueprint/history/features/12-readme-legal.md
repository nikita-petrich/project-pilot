# Feature 12: README & legal

**From build-plan:** feature 12
**Status:** done (2026-07-21)

## Goal

A top-level README that gets a new reader from clone to running, and states the
compliance and legal posture.

## Outcome

`README.md` rewritten from the stub to cover: what project-pilot is and how it
works, requirements, setup (uv sync, `.env`, profile files), the full command
surface, the Docker home-server flow, threshold tuning, troubleshooting (cooldown,
selector breakage, unhealthy container), a development section (the quality gate,
no-live-requests testing), a compliance and legal section (personal use only,
robots.txt gate, user agent, rate limits, no circumvention, 403 cooldown) that
references `docs/compliance.md` and ADR 0001, and the project layout.

## Build steps

- [x] **Step 1 - README (setup, commands, operation, tuning, troubleshooting)**
- [x] **Step 2 - Compliance and legal section referencing docs/compliance.md**

## Notes

Docs-only feature; no code changed and the quality gate stays green.
