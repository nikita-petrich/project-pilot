# Current Feature

> **Generated file.** Holds the one feature or fix being built right now.

# Feature: MCP-Server

**From build-plan:** feature 20
**Status:** built

## Goal

Ein MCP-Server (FastMCP, Streamable HTTP, Bearer-Token) macht project-pilots
Funktionen für jede Claude-Oberfläche aufrufbar: die Routine-Session (Feature 22),
normale Claude-Chats (Custom Connector) und n8n. Zweiter Baustein der
Zielarchitektur.

## In scope

- `src/project_pilot/mcp_server.py`: Tool-Funktionen als explizit komponierte
  Funktionen über den bestehenden Services + FastMCP-Registrierung + Token-Auth.
- Tools: `list_matches`, `get_listing`, `check_listing`, `check_text`,
  `draft_application`, `revise_application`, `set_recipient`,
  `send_application` (nur nach explizitem menschlichem Confirm im Chat),
  `enrich_company`.
- Settings: `MCP_TOKEN`, `MCP_PORT` + `require_mcp()`; `.env.example`.
- CLI: `project-pilot mcp` (uvicorn).
- Repository: `recent_matches(limit)`.
- compose.yaml: eigener `mcp`-Service (gleiches Image, eigener Port).
- Doku: AGENTS.md-Kommando.

## Out of scope

- Reverse-Proxy/TLS/Domain auf dem VPS (Ops, Nik).
- Einbinden als Custom Connector in claude.ai (Niks Account).
- `ingest_listing` für E-Mail-Quellen — braucht eine Design-Entscheidung zur
  `external_url`-Synthese; eigener Folgeschritt (Build-Plan 20b).

## Build steps

- [x] **Step 1 — Tool-Kern + Auth** — Modul mit Deps-Dataclass, Tool-Funktionen,
  FastMCP-Builder, ASGI-Bearer-Middleware. *Done when:* Unit-Tests für
  Tool-Funktionen (Fakes) und Auth-Middleware (401 ohne/mit falschem Token, 200
  mit richtigem) grün.
- [x] **Step 2 — Wiring** — Settings + require_mcp, CLI-Kommando, Repo-Methode,
  compose-Service, .env.example, AGENTS.md. *Done when:* `project-pilot mcp`
  startet lokal (Smoke), alle vier Gates grün.

## Data / contracts

- **Load-bearing:** Tool-Namen wie oben — Routine-Prompt (F22) und n8n rufen sie.
- Tool-Ausgaben: kompakte dicts; `check_*` spiegelt `MatchVerdict`-Felder.
- Neue Spalte kommt erst mit Feature 22 (`claude_session_url`), nicht hier.

## Testing

- Fakes statt Live-Services (bestehendes Muster aus tests/test_slack_bot.py).
- Auth-Middleware über httpx ASGITransport.
- `send_application` wird NICHT gegen echtes SMTP getestet (bestehender Guard in
  ApplicationService ist getestet).

## Notes for the AI

- Tool-Docstrings sind das API-Contract für die LLM-Seite: Was + Wann + Warnung
  bei `send_application` (nur nach explizitem Nutzer-Confirm).
- Kein Framework-Magic: Deps im CLI konstruieren, an Builder übergeben.
- mypy --strict; fastmcp ggf. in mypy-Overrides.
