# Current Feature

> **Generated file.** Holds the one feature or fix being built right now.

# Feature: Routine-Anbindung

**From build-plan:** feature 22
**Status:** built

## Goal

Jeder Match feuert zusätzlich zu Slack die Claude-Routine `match-thread`: eine
Session pro Match, Push aufs Handy über die Claude-App, Session-URL am Listing
gespeichert (Feed-Verlinkung über den MCP-Server). Slack läuft parallel weiter;
sein Abbau ist ein eigener späterer Schritt nach der Feldtest-Phase.

## In scope

- `notification/claude_fire.py`: `ClaudeRoutineFire` (httpx, tenacity-Retry auf
  Netz/5xx — nie auf 4xx), `fire_text(message)` für den kompakten Match-Text.
- Settings: `CLAUDE_FIRE_ENABLED`, `CLAUDE_ROUTINE_FIRE_URL`,
  `CLAUDE_ROUTINE_TOKEN` (secret) + `require_claude_fire()`; `.env.example`.
- DB: `listings.claude_session_url` (nullable) + Alembic-Migration;
  Repo-Methode `set_claude_session_url`.
- Pipeline `_notify`: nach erfolgreichem Send + mark_notified feuern; Fehler
  loggen, nie den Run brechen; nur feuern, wenn noch keine Session-URL steht
  (Doppel-Fire-Guard, da fire keinen Idempotency-Key hat).
- MCP `_listing_summary`: `claude_session_url` mit ausgeben (Feed-Link).
- CLI-Wiring in `_build_pipeline`.

## Out of scope

- Slack-Abbau (eigener Schritt nach 2 Wochen Parallelbetrieb, Build-Plan 23).
- Session-Titel-Setzung (macht der Routine-Prompt selbst).

## Build steps

- [x] **Step 1 — Fire-Client** — Modul + respx-Tests (Erfolg, 5xx-Retry,
  4xx-kein-Retry, Response-Parsing). *Done when:* Tests grün.
- [x] **Step 2 — DB + Pipeline + Wiring** — Migration, Repo, Pipeline-Hook,
  Settings, CLI, MCP-Feld, .env.example. *Done when:* Pipeline-Test beweist:
  Fire nach Send, URL gespeichert, Fire-Fehler bricht nichts, kein Doppel-Fire.

## Data / contracts

- **Load-bearing:** `listings.claude_session_url` — der MCP-Feed und künftige
  Oberflächen lesen es.
- Fire-Request: `POST {url}` mit Bearer + `anthropic-beta:
  experimental-cc-routine-2026-04-01`, Body `{"text": ...}`; Antwort
  `claude_code_session_url`. Experimentelle API → Modul bewusst klein halten.

## Testing

- respx für den Fire-Client; Fake-Fire im Pipeline-Test (bestehendes
  Fake-Notifier-Muster).
- Migration läuft in CI über den bestehenden `alembic upgrade head`-Schritt.
