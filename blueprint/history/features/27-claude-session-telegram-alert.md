# Feature: Claude-Session pro Match, Telegram als Alert (27)

**From build-plan:** feature 27
**Status:** built

## Recherche-Ergebnis (nur offizielle Quellen, Stand 2026-09-09)

**Session pro Match: ja, offiziell.** Der API-Trigger einer Routine
(`POST https://api.anthropic.com/v1/claude_code/routines/{trig_…}/fire`, Beta-Header
`experimental-cc-routine-2026-04-01`) legt pro Aufruf eine neue Cloud-Session im
eigenen Account an und liefert `claude_code_session_id` und
`claude_code_session_url` zurück. Die Session hat die Connectoren der Routine
(also `mcp-project-pilot`), das ausgecheckte Repo mit seinen Skills, und Nik setzt
sie im Browser oder in der Claude-App fort.
Quellen: code.claude.com/docs/en/routines („Add an API trigger“, „Trigger a
routine“), platform.claude.com/docs/en/api/claude-code/routines-fire.
Einschränkungen laut Doku: experimentell, kein Idempotency-Key (jeder POST = neue
Session), Tages-Kontingent an Routine-Runs, 429 bei Erschöpfung.

**Benachrichtigung durch Claude: nein, nicht zuverlässig.** Die Doku kennt Push
nur unter zwei Bedingungen, beide nicht erfüllbar:

- Remote Control (code.claude.com/docs/en/remote-control, „Mobile push
  notifications“): „When Remote Control is active, Claude can send push
  notifications to your phone. Claude decides when to push. … Beyond the two
  on/off toggles below, there is no per-event configuration.“ Remote Control
  heißt: eine lokal laufende CLI auf Niks Rechner — nicht eine vom Worker
  erzeugte Cloud-Session.
- Dispatch (code.claude.com/docs/en/mobile): benachrichtigt, wenn eine von
  Dispatch gestartete Desktop-Session fertig ist — Desktop-App muss laufen, und
  Dispatch wird vom Handy aus angestoßen, nicht per API.

Weder die Routines-Doku noch die Cloud-Sessions-Doku noch die
Cloud-Environments-Doku erwähnen eine Benachrichtigung für eine per API
erzeugte Session. Das `PushNotification`-Tool ist laut eigener Beschreibung eine
Terminal-Benachrichtigung, die nur bei verbundenem Remote Control aufs Handy geht.
Genau dieses Verhalten hat das Projekt schon einmal erlebt (Feature 22/23: Push
„wenn Claude es für meldenswert hält“, ausgefallen; Issues #60005/#60208 als
„not planned“ geschlossen).

**Entscheidung:** Claude erzeugt die Session (Routine-Fire), Telegram liefert den
Alert. Der Worker garantiert die Zustellung selbst.

## Goal

Ein Match = eine Claude-Session in Niks Account plus eine Telegram-Nachricht mit
der bekannten Übersicht und drei Buttons:

| Button | Tut |
|---|---|
| 📄 Projektbeschreibung öffnen | URL-Button auf die Original-Ausschreibung |
| ✅ Bewerben | URL-Button auf die Session (`https://claude.ai/code/session_…`) |
| 🚫 Ablehnen | Callback: der Bot löscht die Nachricht aus dem Chat |

In der Session arbeitet Nik mit den Account-Skills und den `project_pilot_*`-Tools
(Prüfen, Entwurf, Empfänger, Versand mit ausdrücklicher Bestätigung). Alles, was
Telegram zur Arbeitsfläche gemacht hatte (Forum-Topics, Thread-Agent, Agent SDK),
fällt weg.

## In scope

- `notification/claude_fire.py`: `ClaudeRoutineFire.open_session(message)` —
  ein POST mit Retry (Netz/5xx/429, nie 4xx), liefert die Session-URL oder `None`.
- `notification/telegram.py`: Karte wie bisher, Keyboard aus zwei URL-Buttons
  und einem Callback; `notify(message, session_url=…)`.
- `telegram_bot.py` neu und klein: Long-Polling nur auf `callback_query`,
  `decline:<id>` → `deleteMessage` (Fallback nach 48 h: Buttons entfernen und
  „🚫 Abgelehnt“ voranstellen). Kein Agent, keine Datenbank, keine Whitelist —
  der Chat ist der private Chat mit dem Bot, die Chat-ID ist die Prüfung.
- Pipeline `_notify`: erst Fire (nur wenn noch keine Session-URL am Listing;
  URL sofort committen — Doppel-Fire-Guard), dann Karte, dann `notified_at`.
  Schlägt der Fire fehl, geht die Karte trotzdem raus (ohne Bewerben, mit
  Hinweis und Listing-ID) — der Alert ist die Priorität, wie beim Umzug weg vom
  Routine-Push gelernt.
- DB: `listings.claude_session_url` zurück, `telegram_threads` weg (eine Migration).
- Settings: `CLAUDE_ROUTINE_FIRE_URL`, `CLAUDE_ROUTINE_TOKEN`, `require_claude_fire()`.
- `test-match`: Fire + Push, speichert nichts.
- MCP `list_matches`/`get_listing`: `claude_session_url` mit ausgeben.

## Gelöscht

- `agent.py`, der bisherige `telegram_bot.py`, `tests/test_agent.py`,
  `tests/test_telegram_bot.py`, `tests/test_channel_flow_e2e.py`
- `TelegramThread` + alle Thread-Methoden im Repository
- Dependencies `claude-agent-sdk`, `anthropic`, `aiohttp`
- Settings `ANTHROPIC_API_KEY`, `AGENT_MODEL`, `AGENT_WORKSPACE`, `MCP_URL`,
  `TELEGRAM_ALLOWED_USER_IDS`
- Compose: `agentdata`-Volume, `CLAUDE_CONFIG_DIR`, MCP-Abhängigkeit des Bots;
  Dockerfile: `/data`

## Out of scope

- Ablehnen archiviert die Claude-Session nicht (dafür gibt es keine öffentliche API).
- Ein zweiter Telegram-Chat oder Kanal; `TELEGRAM_CHAT_ID` ist der private Chat.

## Build steps

- [x] **Step 1 — Recherche** — offizielle Quellen, Ergebnis oben.
- [x] **Step 2 — Fire-Client + Karte + Poller** — `claude_fire.py`, `telegram.py`,
      `telegram_bot.py`, Tests (respx).
- [x] **Step 3 — Pipeline, DB, Config, CLI, MCP, Selftest** — Migration,
      Repository, `_notify`, Settings, Wiring, Tests.
- [x] **Step 4 — Rückbau** — Agent, Threads, Dependencies, Compose, Dockerfile,
      Deploy-Gate.
- [x] **Step 5 — Doku** — `docs/claude-setup.md` (Routine anlegen, Prompt, Bot),
      README, AGENTS.md, `.env.example`, deployment.md, overview, build-plan.

## Data / contracts

- `listings.claude_session_url` (String 512, nullable) — load-bearing: der
  Doppel-Fire-Guard und der MCP-Feed lesen es.
- Fire-Request/-Response wie in der offiziellen Referenz; Antwortfeld
  `claude_code_session_url`.
- Telegram-Callback `decline:<listing_id>`; die Nachricht selbst trägt ihre
  `message_id`, mehr braucht der Bot nicht.

## Testing

- respx für Fire-Client, Notifier und Poller; Fake-Fire und Fake-Notifier in
  den Pipeline-Tests (Reihenfolge, Guard, Fire-Fehler → Karte trotzdem).
- Migration up/down gegen Postgres 16 (`alembic upgrade head` im Gate).

## Ergebnis

Quality gate grün gegen Postgres 16: `ruff check`, `ruff format --check`,
`mypy --strict`, `alembic upgrade head` (plus `downgrade -1` und zurück),
464 Tests, 89 % Coverage. Gelöscht: `agent.py`, der Thread-Bot, drei
Testmodule, `TelegramThread`, neun Repository-Methoden, drei Dependencies, fünf
Settings, das `agentdata`-Volume. Neu: `claude_fire.py` (105 Zeilen),
`telegram_bot.py` (180 Zeilen statt 976), eine Migration.

Offen für Nik (Betrieb, kein Code): Routine `match-thread` mit dem Prompt aus
`docs/claude-setup.md` anlegen, API-Trigger erzeugen, `CLAUDE_ROUTINE_FIRE_URL`
und `CLAUDE_ROUTINE_TOKEN` ins `prod`-Environment, `TELEGRAM_CHAT_ID` auf den
privaten Chat mit dem Bot umstellen, dann `test-match`.
