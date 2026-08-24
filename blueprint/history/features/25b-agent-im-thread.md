# Feature: Agent im Thread (25b)

**From build-plan:** feature 25b (of 25, Telegram-Thread-Agent)
**Status:** done

## Goal

Im Match-Thread antwortet Claude. Du schreibst „passt das wirklich?" oder
„schreib die Bewerbung", und der Agent prüft, entwirft, überarbeitet und sendet
— über die `project_pilot`-MCP-Tools, ohne dass du Telegram verlässt.

## Architekturentscheidung: MCP-Connector statt Agent SDK

Das Briefing sah das **Claude Agent SDK** vor. Ich baue stattdessen auf die
**Messages API mit MCP-Connector** (`mcp_servers` + `mcp_toolset`, Beta
`mcp-client-2025-11-20`). Gründe, in der Reihenfolge ihres Gewichts:

1. **Sicherheit.** Das Agent SDK bringt Read/Write/Edit/Bash mit; die müsste man
   mühsam wegkonfigurieren. Der MCP-Connector hat sie gar nicht — der Agent
   kennt ausschließlich die zehn `project_pilot`-Tools. Das „Bot mit Bash =
   offene Shell"-Risiko aus dem Briefing existiert nicht.
2. **Betrieb.** Das Agent SDK startet die Claude-Code-CLI als Subprozess: Node
   plus CLI müssten ins Image. Der Connector ist ein HTTPS-Aufruf.
3. **Zustand.** Keine `.jsonl`-Sessions unter `~/.claude/projects/`, kein
   Cross-Host-Problem, kein `session_store`-Adapter. Der Verlauf liegt in
   Postgres, wo schon alles andere liegt.

Anthropics Server rufen unseren MCP-Server direkt auf — er ist ohnehin
öffentlich erreichbar und tokengesichert.

## In scope

- Neuer Prozess `project-pilot telegram-bot`: Long-Polling (`getUpdates`), kein
  eingehender Port
- `user_id`-Whitelist; alles andere wird verworfen und geloggt
- Router: `message_thread_id` → Listing → Verlauf → Antwort in denselben Thread
- Agent über die Messages API mit MCP-Connector, **nur** `project_pilot`-Tools
- Verlauf pro Thread in Postgres (`telegram_threads.history`), begrenzt
- Sofortiges Lebenszeichen: `sendChatAction("typing")` vor dem Modellaufruf
- Antworten über 4096 Zeichen werden gestückelt
- Ein Lock pro Thread: zwei schnelle Nachrichten laufen nacheinander, nicht
  gleichzeitig
- `ANTHROPIC_API_KEY` als Pflicht-Secret im Deploy-Gate

## Out of scope

- Inline-Buttons, `setMyCommands` (25c)
- Anhänge, `/reset`, Kostendeckel, Retention (25d)
- Bilder und PDFs im Thread (25d)

## Build steps

- [x] **Step 1 – Verlauf am Thread** – `history` (JSONB) und `updated_at` an
      `telegram_threads`, Migration, `get_thread_by_thread_id` und
      `append_history` im Repository.
      *Done when:* Migration up/down gegen echtes Postgres, ein Test schreibt
      zwei Runden Verlauf und liest sie zurück, ein Test findet den Thread über
      die `thread_id`.

- [x] **Step 2 – Der Agent** – `agent.py`: ein Aufruf gegen die Messages API mit
      MCP-Connector, Systemprompt nennt Listing-ID und Regeln, Verlauf rein,
      Text raus.
      *Done when:* respx-Tests belegen: `mcp_servers` und `mcp_toolset` tragen
      denselben Servernamen, der Beta-Header ist gesetzt, der Systemprompt nennt
      die Listing-ID, der Verlauf wird mitgeschickt und auf die letzten N Runden
      begrenzt, und ein API-Fehler gibt eine lesbare Meldung statt zu werfen.

- [x] **Step 3 – Der Bot-Prozess** – `telegram_bot.py`: Polling-Schleife,
      Whitelist, Routing über `message_thread_id`, typing, Chunking, Lock pro
      Thread; CLI-Kommando `telegram-bot`.
      *Done when:* Tests belegen: eine fremde `user_id` wird verworfen, eine
      Nachricht ohne bekannten Thread wird höflich abgelehnt, eine bekannte
      landet beim Agenten und die Antwort geht in denselben Thread, `offset`
      wird korrekt fortgeschrieben, und eine lange Antwort wird gestückelt.

- [x] **Step 4 – Betrieb** – Settings, Deploy-Gate (`ANTHROPIC_API_KEY`),
      `compose.prod.yaml`-Service, `.env.example`, Doku.
      *Done when:* das Gate weist ein fehlendes `ANTHROPIC_API_KEY` ab, der
      Compose-Service startet den Bot, und `uv run pytest` ist grün.

## Data / contracts

`telegram_threads` wächst um:

| Spalte | Typ | Bedeutung |
|---|---|---|
| `history` | JSONB, default `[]` | `[{"role": "user"|"assistant", "text": "…"}]` |
| `updated_at` | timestamptz | letzte Aktivität im Thread |

**Bewusst nur Text im Verlauf**, keine Tool-Blöcke: Die Tools sind die
Wahrheitsquelle, jede Runde liest den Stand frisch aus der Datenbank. Das hält
den Verlauf klein, robust gegen API-Formatänderungen und frei von
Wiedergabe-Fallstricken.

## Notes for the AI

- Sending bleibt hart gegated: der Systemprompt verlangt die ausdrückliche
  Bestätigung vor `project_pilot_send_application`, wie der MCP-Prompt.
- Kein Bash, kein Dateisystem, keine anderen Tools — das ist die Zusage dieser
  Architektur und darf nicht aufgeweicht werden.
- Der Bot antwortet **nur** in Threads, die zu einem Listing gehören.
- Fehler des Modells oder der API werden dem Nutzer im Thread mitgeteilt, nicht
  verschluckt.
