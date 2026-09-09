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

## Nachtrag: Deep Link statt Routine

Nach dem ersten Umbau kam die Frage, ob die Routine überhaupt nötig ist. Ist
sie nicht: `https://claude.ai/code/new?q=…&repo=…` füllt eine neue Session vor
(offiziell: web-quickstart „Pre-fill sessions“, Support-Artikel „Open the Claude
mobile app with a link“), die Claude-App öffnet den Link nativ. Die Session
entsteht erst beim Senden — kein Run für abgelehnte Matches, kein Tages-Limit,
kein Token, keine experimentelle API, nichts am Listing zu speichern.

Der Prompt im Link ist die Karte aus der Telegram-Nachricht (zeichengleich),
dann ein kurzer Auftrag (Listing-ID, `get_listing`, Karte wiedergeben, fünf
Bullets, warten). Ein Link über 2.048 Zeichen (realistisch: ~1.900) lässt die
Karte weg und bittet die Session, sie aus der Datenbank zu rendern.

Gruppierung in der App: Code-Sessions kennen laut Doku keine Tags oder Ordner —
nur Titel, Archivieren, Filter. Der Titel entsteht aus dem Prompt-Anfang, also
`⭐ 87 · Rolle · Firma`; das Repo steht an jeder Session. Projects gruppieren nur
Chats, und kein dokumentierter Link öffnet einen vorausgefüllten Chat *in* einem
Project.

## Goal

Ein Match = eine Telegram-Nachricht mit der bekannten Übersicht und drei
Buttons; **Bewerben** öffnet eine neue Claude-Session mit derselben Übersicht
im Prompt:

| Button | Tut |
|---|---|
| 📄 Projektbeschreibung öffnen | URL-Button auf die Original-Ausschreibung |
| ✅ Bewerben | URL-Button `claude.ai/code/new?q=<Karte + Auftrag>&repo=<Repo>` |
| 🚫 Ablehnen | Callback: der Bot löscht die Nachricht aus dem Chat |

In der Session arbeitet Nik mit den Repo- und Account-Skills und den
`project_pilot_*`-Tools (Prüfen, Entwurf, Empfänger, Versand mit ausdrücklicher
Bestätigung). Alles, was Telegram zur Arbeitsfläche gemacht hatte (Forum-Topics,
Thread-Agent, Agent SDK), fällt weg.

## In scope

- `notification/claude_link.py`: `session_prompt(message)` (Karte + Auftrag,
  Kurzform ohne Karte für den Überlängen-Fall), `session_link(message, repo=…)`.
- `notification/telegram.py`: Karte wie bisher; Keyboard aus zwei URL-Buttons
  (Session-Link, Listing) und einem Callback.
- `telegram_bot.py` neu und klein: Long-Polling nur auf `callback_query`,
  `decline:<id>` → `deleteMessage` (Fallback nach 48 h: Buttons entfernen und
  „🚫 Abgelehnt“ voranstellen). Kein Agent, keine Datenbank, keine Whitelist —
  der Chat ist der private Chat mit dem Bot, die Chat-ID ist die Prüfung.
- Pipeline `_notify` unverändert im Kern: Karte senden, `notified_at` nur bei
  Erfolg.
- DB: `telegram_threads` weg (eine Migration).
- Settings: `CLAUDE_SESSION_REPO` (Default dieses Repo).
- `test-match`: echte Karte mit echtem Bewerben-Button — Telegram prüft die
  Button-URL beim Senden, eine zugestellte Karte beweist den Link.

## Gelöscht

- `agent.py`, der bisherige `telegram_bot.py`, `tests/test_agent.py`,
  `tests/test_telegram_bot.py`, `tests/test_channel_flow_e2e.py`
- `TelegramThread` + alle Thread-Methoden im Repository
- Dependencies `claude-agent-sdk`, `anthropic`, `aiohttp`
- Settings `ANTHROPIC_API_KEY`, `AGENT_MODEL`, `AGENT_WORKSPACE`, `MCP_URL`,
  `TELEGRAM_ALLOWED_USER_IDS`
- Compose: `agentdata`-Volume, `CLAUDE_CONFIG_DIR`, MCP-Abhängigkeit des Bots;
  Dockerfile: `/data`
- Der Zwischenstand mit Routine-Fire (`claude_fire.py`, `CLAUDE_ROUTINE_*`,
  `listings.claude_session_url`) — gebaut und im selben Branch wieder ersetzt

## Out of scope

- Ablehnen archiviert keine Claude-Session (es gibt keine, bevor Nik tippt).
- Ein zweiter Telegram-Chat oder Kanal; `TELEGRAM_CHAT_ID` ist der private Chat.

## Build steps

- [x] **Step 1 — Recherche** — offizielle Quellen, Ergebnis oben.
- [x] **Step 2 — Link-Builder + Karte + Poller** — `claude_link.py`, `telegram.py`,
      `telegram_bot.py`, Tests (respx).
- [x] **Step 3 — Pipeline, DB, Config, CLI, Selftest** — Migration, Repository,
      Settings, Wiring, Tests.
- [x] **Step 4 — Rückbau** — Agent, Threads, Dependencies, Compose, Dockerfile,
      Deploy-Gate.
- [x] **Step 5 — Doku** — `docs/claude-setup.md`, README, AGENTS.md,
      `.env.example`, deployment.md, overview, build-plan.

## Data / contracts

- Deep Link: `https://claude.ai/code/new?q=<prompt>&repo=<owner/name>`,
  Obergrenze 2.048 Zeichen (`MAX_URL_CHARS`), sonst Kurzprompt.
- Telegram-Callback `decline:<listing_id>`; die Nachricht selbst trägt ihre
  `message_id`, mehr braucht der Bot nicht.

## Testing

- respx für Notifier und Poller; Unit-Tests für Prompt und Link (Karte
  zeichengleich mit der Telegram-Nachricht, Listing-ID, ASCII-sicher,
  Längenlimit mit Fallback); Fake-Notifier in den Pipeline-Tests.
- Migration up/down gegen Postgres 16 (`alembic upgrade head` im Gate).

## Ergebnis

Quality gate grün gegen Postgres 16: `ruff check`, `ruff format --check`,
`mypy --strict`, `alembic upgrade head` (plus `downgrade -1` und zurück gegen
eine geleerte Datenbank), 458 Tests, 89 % Coverage. Gelöscht: `agent.py`, der
Thread-Bot, drei Testmodule, `TelegramThread`, neun Repository-Methoden, drei
Dependencies, fünf Settings, das `agentdata`-Volume. Neu: `claude_link.py`
(~90 Zeilen), `telegram_bot.py` (180 Zeilen statt 976), eine Migration.

Offen für Nik (Betrieb, kein Code): `TELEGRAM_CHAT_ID` auf den privaten Chat
mit dem Bot umstellen, Claude-App auf dem Handy mit demselben Account, dann
`test-match` und einmal auf Bewerben tippen.
