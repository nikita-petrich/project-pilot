# Current Feature

> **Generated file.** Holds the one feature or fix being built right now.

# Feature: Slack-Abbau — Claude-Routine wird der einzige Kanal

**From build-plan:** feature 23
**Status:** built

## Goal

Slack vollständig entfernen. Die match-thread-Routine ist ab jetzt DER
Benachrichtigungskanal (Matches und Betriebswarnungen), ohne Feature-Flag:
Konfiguration ist Pflicht, fehlende Secrets lassen den **Deploy** scheitern
(render-env.py), nicht den Server crash-loopen.

## In scope

- Pipeline: Match-Versand = Routine-Fire (URL speichern + `notified_at` nur bei
  Erfolg — dieselbe Durability-Garantie wie vorher); Warnungen (Cooldown,
  LLM-Health, consecutive failures) → `fire_warning` (eigene Session mit Push).
- `claude_fire.py`: `fire_warning(text)`.
- Löschen: `notification/slack.py`, `slack_bot.py`, `bot`- und
  `test-notify`-Kommandos, Slack-Settings/`SlackConfig`, `slack-sdk`-Dependency,
  Slack-Tests, tote Draft-Ref-Methoden (bot-only).
- `CLAUDE_FIRE_ENABLED` entfernt — Config-Präsenz ist Pflicht
  (`require_claude_fire()` in `_build_pipeline`).
- `selftest`/`test-match`: Ende-zu-Ende-Beweis feuert jetzt die Routine.
- compose: mcp-Service ohne Opt-in-Profil; `compose.prod.yaml` bekommt den
  mcp-Service (edge-Netz, Alias `project-pilot-mcp`).
- `deploy/render-env.py`: REQUIRED += `CLAUDE_ROUTINE_FIRE_URL`,
  `CLAUDE_ROUTINE_TOKEN`, `MCP_TOKEN`; SLACK_* raus.
- Doku: AGENTS.md, .env.example, project-overview (Slack-Aussagen), Build-Plan 23.

## Out of scope

- `applications.draft_ref`-Spalte bleibt (historische Daten); nur die toten
  Zugriffsmethoden fallen.
- MCP-Reverse-Proxy-Routing auf dem VPS (Ops).

## Build steps

- [x] **Step 1 — Kanal umbauen** — pipeline + claude_fire + Tests.
- [x] **Step 2 — Slack löschen** — Module, CLI, Config, Deps, Tests, selftest.
- [x] **Step 3 — Deploy & Doku** — render-env, compose(s), AGENTS, .env.example,
  overview. *Done when:* alle vier Gates grün gegen echtes Postgres, Grep nach
  `slack` in `src/` und `tests/` leer.
