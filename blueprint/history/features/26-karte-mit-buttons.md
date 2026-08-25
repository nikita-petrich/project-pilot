# Feature: Karte mit Aktions-Buttons (26)

**From build-plan:** feature 26
**Status:** done

## Goal

Ein Match ist eine Nachricht in Telegram, die das ganze Listing zeigt, unter
drei Buttons: Projekt öffnen, Annehmen, Abnehmen. Annehmen schreibt sofort die
Bewerbung und verweist ins Claude-Projekt, wo sie mit den Skills fertiggemacht
wird. Abnehmen löscht die Nachricht.

Damit wandert das Gespräch zurück nach Claude und Telegram wird zur
Entscheidungsfläche — genau eine Aufgabe, die es gut kann.

## Was zurückgebaut wurde

Feature 25 hatte Telegram zur Arbeitsfläche gemacht: ein Forum-Topic pro Match
(25a) und ein Claude-Agent im Thread (25b). Beides ist mit 26 entfallen:

- `telegram_threads` (Tabelle, Zuordnung, Verlauf) — Migration `f3a7d195c204`
- `agent.py` und die `anthropic`-Abhängigkeit
- `ANTHROPIC_API_KEY`, `MCP_PUBLIC_URL`, `AGENT_MODEL`
- `createForumTopic` und alles, was `message_thread_id` betraf

Der `telegram-bot`-Prozess bleibt, macht aber nur noch eine Sache: er hört auf
`callback_query`.

## In scope

- Karte mit dem ganzen Listing: Headline, Match-Karte, Fakten, Beschreibung
  (gekürzt statt abgeschnitten)
- Drei Buttons; die Listing-ID reist in jedem Callback mit (`accept:42`)
- Annehmen → `ApplicationService.draft_for_listing`, dann Karte umschreiben auf
  Bewerbungs-ID und die zu tippenden Kommandos, mit einem Button ins Projekt
- Abnehmen → Nachricht löschen
- `user_id`-Whitelist, Chat-Prüfung
- Der Bot-Prozess wird **ohne Mailer** verdrahtet: Senden ist von dort aus
  technisch unmöglich, nicht nur unerwünscht

## Out of scope

- Anhänge an den Bot (25d)
- `setMyCommands`, Statusfarben

## Build steps

- [x] **Step 1 – Rückbau** – Agent, Thread-Tabelle, Topics und die Agent-Secrets
      entfernt; Migration `f3a7d195c204` prüft up und down.
- [x] **Step 2 – Karte und Buttons** – `match_text` trägt das ganze Listing,
      `match_keyboard` die drei Buttons.
- [x] **Step 3 – Button-Handler** – `TelegramButtons`: Callback-Parsing,
      Whitelist, Annehmen entwirft, Abnehmen löscht.
- [x] **Step 4 – Betrieb** – CLI, Compose, `.env.example`, Doku.

## Testing

447 Tests grün gegen echtes Postgres 16, 89 % Coverage. Belegt unter anderem:
die Listing-ID im Callback, dass Abnehmen nie entwirft, dass ein Fremder nichts
drücken kann, dass ein fehlgeschlagener Entwurf sichtbar wird statt zu
verschwinden, und dass der Offset auch über verworfene Updates hinweg
fortschreitet.

## Notes for the AI

- Ein Match ist eine Nachricht, kein Thread. Wer wieder Topics einbaut, dreht 26
  zurück.
- Die Listing-ID gehört in jedes Callback — nie ein „aktuelles" Listing raten.
- Der Bot entwirft, er sendet nicht. Der fehlende Mailer ist die Zusage.
