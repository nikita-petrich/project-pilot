# Fix: zurück zum Thread pro Match

**Type:** revert
**Status:** done
**Reverts:** feature 26 (`blueprint/history/features/26-karte-mit-buttons.md`)

## Was

Feature 26 hatte den Match auf eine Nachricht mit drei Buttons reduziert und das
Arbeiten daran zurück nach Claude verlegt. Das ist wieder zurückgenommen: ein
Match ist wieder ein eigenes Forum-Topic (25a), in dem ein Agent mit ausschließlich
den `project_pilot`-MCP-Tools antwortet (25b).

## Warum

Der Punkt der Threads war, den Match nicht aus Telegram herausfallen zu lassen.
Mit der Karte war die Entscheidung zwar in Telegram, die Arbeit daran aber wieder
in einer zweiten App — genau der Sprung, den die Threads abgeschafft hatten.

## Wie

Zwei `git revert`-Commits (`d8183a6`, `9e4f47c`) stellen Code, Tests, Konfiguration
und Dokumentation wieder her. Die Migration ist die eine Ausnahme: `f3a7d195c204`
(Drop von `telegram_threads`) bleibt stehen, weil die Produktionsdatenbank sie
bereits ausgeführt hat und ihr `alembic_version` sie nennt. Die Tabelle kommt
stattdessen vorwärts über eine neue Migration `d2c4b8e17a63` zurück.

## Wieder scharf

- `telegram_threads` (Zuordnung `listing_id ↔ thread_id` plus Verlauf)
- `agent.py` und die `anthropic`-Dependency
- `ANTHROPIC_API_KEY`, `MCP_PUBLIC_URL`, `AGENT_MODEL`
- `createForumTopic` je Match; `TELEGRAM_CHAT_ID` muss wieder eine Forum-Supergruppe sein
