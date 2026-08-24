# Feature: Topic pro Match (25a)

**From build-plan:** feature 25a (of 25, Telegram-Thread-Agent)
**Status:** done

## Goal

Jeder Match bekommt seinen eigenen Forum-Topic in einer privaten
Telegram-Supergruppe, statt in einem einzigen Chat unterzugehen. Der Topic-Titel
ist die Match-Zeile, die Karte steht darin, und die Zuordnung
`thread_id ↔ listing_id` liegt in Postgres.

Das ist für sich genommen schon besser als heute (ein Projekt = ein Thread,
Telegram-eigenes Archiv über „Topic schließen"), und es ist die Voraussetzung
für 25b: dort wird `message_thread_id` zum Schlüssel der Agent-Session. Ohne
belastbare Zuordnung gibt es keine Session pro Projekt.

Kein Agent, kein Bot-Prozess, keine eingehenden Nachrichten in diesem Schritt —
der Worker sendet weiterhin nur.

## In scope

- Umstellung des Sendeziels auf eine **Forum-Supergruppe** (nur Nik + Bot,
  Topics aktiviert, Bot Admin mit `can_manage_topics`)
- `createForumTopic` pro Match; Titel aus der Headline, `icon_color` als
  Statusfarbe (neu = blau)
- Karte in den erzeugten Thread (`message_thread_id`)
- Tabelle `telegram_threads` (`thread_id`, `listing_id`, `created_at`) plus
  Alembic-Migration und Repository-Methoden — **load-bearing für 25b**
- Idempotenz: ein Listing bekommt genau einen Topic, auch wenn ein Lauf
  wiederholt wird
- Fallback, wenn Topics nicht verfügbar sind (Chat ist kein Forum, Bot ist kein
  Admin): senden ohne `message_thread_id`, laut geloggt, Lauf bleibt grün
- Betriebswarnungen bleiben im „General"-Bereich, ohne eigenen Topic
- `.env.example`, Deploy-Gate (Supergruppen-ID ist negativ), `docs/claude-setup.md`

## Out of scope

- Der Bot-Prozess, Long-Polling, eingehende Nachrichten (25b)
- Der Claude-Agent und alles zu Sessions (25b)
- Inline-Buttons, `setMyCommands`, zweite Bestätigung (25c)
- Topic schließen bei „erledigt", Statusfarbe über blau hinaus (25c)
- Anhänge, Chunking, Retention, Kostendeckel (25d)
- `ANTHROPIC_API_KEY` — wird erst in 25b gebraucht

## Build loop

Build one step at a time, never the whole feature at once.

1. Plan mode lays out the step before any code.
2. The AI implements just that step.
3. It shows the diff (not full files); you read it and understand it.
4. You approve, then choose whether to commit a checkpoint or roll straight on.
   Checkpoints are optional; `/complete` makes the real feature-level commit at the end.

Never accept a step you haven't read. If a diff is too big to review, the step was too big, so split it.

## Build steps

- [x] **Step 1 – `telegram_threads` + Repository** – Modell, Alembic-Migration
      (up und down gegen echtes Postgres 16 geprüft), `get_thread(listing_id)`
      und `record_thread(listing_id, thread_id)` im Repository.
      *Done when:* `alembic upgrade head` und `downgrade -1` laufen sauber
      durch, ein Repository-Test legt eine Zuordnung an und liest sie zurück,
      und ein zweiter Aufruf für dasselbe Listing legt keine zweite Zeile an.

- [x] **Step 2 – `createForumTopic` im Notifier** – `create_topic(name, icon_color)`
      in `notification/telegram.py`, Titel aus `headline()` auf Telegrams
      128-Zeichen-Grenze gekürzt, `notify(..., thread_id=…)` sendet in den Thread.
      *Done when:* respx-Tests belegen: der Aufruf geht an `createForumTopic`
      mit Name und `icon_color`, die zurückgegebene `message_thread_id` wird
      geliefert, ein zu langer Titel wird gekürzt statt abgelehnt, und ein
      Fehler (kein Forum / kein Admin) gibt `None` zurück ohne zu werfen.

- [x] **Step 3 – Pipeline: ein Topic pro Match** – im `_notify`-Zweig zuerst
      `create_topic`, dann die Karte hinein, dann Zuordnung speichern und
      `notified_at` setzen — alles vor dem Commit, damit ein Abbruch nicht
      „Topic da, Zuordnung fehlt" hinterlässt. Kein Topic erzeugbar → in den
      General-Bereich senden, Warnung loggen.
      *Done when:* ein Pipeline-Test zeigt für zwei Matches genau zwei Topics
      mit zwei gespeicherten Zuordnungen; ein zweiter Lauf erzeugt keinen
      weiteren Topic; im Fallback-Fall wird trotzdem gesendet und der Lauf
      bleibt `success`.

- [x] **Step 4 – Konfiguration und Doku** – `.env.example` (Supergruppen-ID
      erklären), Deploy-Gate prüft die Chat-ID-Form (Supergruppen sind negativ,
      `-100…`), `docs/claude-setup.md` mit der Einrichtung der Gruppe.
      *Done when:* `python3 deploy/render-env.py` weist eine positive Chat-ID
      mit klarer Meldung ab, die Doku beschreibt Gruppe anlegen → Topics an →
      Bot als Admin mit `can_manage_topics` → ID auslesen, und `uv run pytest`
      ist grün.

## Files / areas

| Datei | Was |
|---|---|
| `src/project_pilot/models.py` | `TelegramThread`-Entity |
| `alembic/versions/*_telegram_threads.py` | neue Tabelle |
| `src/project_pilot/repository.py` | `get_thread`, `record_thread` |
| `src/project_pilot/notification/telegram.py` | `create_topic`, `thread_id` beim Senden |
| `src/project_pilot/pipeline.py` | Topic-Erzeugung im Notify-Zweig |
| `src/project_pilot/config.py` | Kommentar/Validierung zur Supergruppen-ID |
| `deploy/render-env.py` | Formprüfung der Chat-ID |
| `.env.example`, `docs/claude-setup.md` | Einrichtung |
| `tests/test_telegram.py`, `tests/test_pipeline.py`, `tests/test_repository.py` | Tests |

## Data / contracts

**`telegram_threads`** — load-bearing, 25b baut darauf auf:

| Spalte | Typ | Bedeutung |
|---|---|---|
| `listing_id` | fk → `listings`, **unique** | ein Listing, ein Thread |
| `thread_id` | int | Telegrams `message_thread_id` |
| `created_at` | timestamptz | wann der Topic entstand |

`session_id` und `state` kommen in 25b dazu — hier bewusst noch nicht, damit die
Spalten nicht leer mitlaufen. Die Unique-Constraint auf `listing_id` ist der
Idempotenz-Guard.

Telegram-Grenzen, die im Code auftauchen: Topic-Titel max. 128 Zeichen,
`icon_color` nur sechs feste Werte (neu = blau `7322096`).

## Testing

`uv run pytest` ist das Gate (siehe `AGENTS.md`), also bekommt jeder
logiktragende Schritt seinen Test im selben Diff:

- **Step 1** – Repository gegen die Test-Datenbank: anlegen, zurücklesen, kein
  Duplikat. Migration zusätzlich manuell up/down gegen ein Scratch-Postgres 16.
- **Step 2** – respx gegen die Bot-API: Titelkürzung, `icon_color`,
  zurückgegebene `message_thread_id`, Fehlerfall gibt `None`.
- **Step 3** – Pipeline mit Fake-Notifier: ein Topic pro Match, Zuordnung
  gespeichert, kein zweiter Topic beim Wiederholungslauf, Fallback bleibt grün.
- **Step 4** – Gate-Test für die Chat-ID-Form.

Keine Live-Requests; die Bot-API wird wie bisher mit respx gemockt.

Manuell am Ende (`/check`): `docker compose exec app project-pilot test-match`
→ die Nachricht landet im General-Bereich der Gruppe (test-match speichert
nichts, hat also kein Listing und braucht keinen Topic). Ein echter Match legt
seinen Topic an.

## Notes for the AI

- **Reihenfolge im Notify-Zweig (beim Bauen korrigiert):** Topic erzeugen →
  Zuordnung **sofort committen** → senden → `notified_at` → Commit. Die Spec
  hatte die Zuordnung erst nach dem Senden vorgesehen; das war falsch: ein
  Topic ist ein externer Seiteneffekt, den kein Rollback zurücknimmt, also
  hätte ein fehlgeschlagener Versand die Zuordnung verloren und der nächste
  Lauf ein zweites Topic für dasselbe Projekt geöffnet.
- **Kein Abbruch bei Telegram-Fehlern.** Wie heute: Fehler heißt „nicht
  benachrichtigt", der nächste Lauf holt es nach. Der Lauf selbst bleibt grün.
- **`create_topic` gibt `None` statt zu werfen** — konsistent mit `notify`.
- Supergruppen-IDs sind negativ (`-100…`); ein positiver Wert ist eine
  Nutzer-ID und ein typischer Konfigurationsfehler.
- Betriebswarnungen bekommen **keinen** Topic — sie gehören keinem Listing.
- Bestehende Konventionen: async durchgehend, `Mapped[...]`/`mapped_column`,
  keine `Any`, Docstrings sagen was und warum, kein `print`.
- Kein Modell und kein API-Key in diesem Sub-Feature. Wer hier einen Agenten
  einbaut, hat 25b vorgezogen.
