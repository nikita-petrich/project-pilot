# Current Fix: Bewerben-Prompt wird Briefing statt Kurzbrief

> Ad-hoc-Änderung, nicht aus dem Build-Plan. `/complete` archiviert sie nach
> `blueprint/history/fixes/`.

## Problem

Der Prompt hinter dem Bewerben-Button (`notification/claude_link.py`) war ein
Kurzbrief: Karte, `Listing-ID`, "schreib maximal 5 Bullets, dann warte auf
mich". Drei Dinge stimmten daran nicht:

1. **Der Chat tut das Falsche.** Nik tippt Bewerben, weil er die Bewerbung
   will — und bekommt fünf Bullets, die die Karte paraphrasieren, die er
   gerade gelesen hat. Danach muss er den Entwurf erst anfordern.
2. **Das URL-Budget war zu klein.** `MAX_URL_CHARS = 2_048` reichte für eine
   voll gefüllte Karte nicht (gemessen: 2.238 Zeichen), also fiel der Link
   still auf die kartenlose Kurzform zurück. Genau die reichen Listings, bei
   denen die Fakten am meisten helfen, verloren sie.
3. **Der Prompt trug zu wenig.** Keine DB-Koordinaten außer der ID, keine
   Schwelle, keine absolute Uhrzeit, keine Recherche-Links — alles Dinge, die
   Nik danach von Hand zusammensucht, und Tools, die das Modell raten musste.

## Ziel

Ein Prompt für zwei Leser: Nik liest die obere Hälfte auf dem Handy (Karte +
Kontext + Links), das Modell die untere (Auftrag + Tool-Inventar + Ausgabe-
regeln). Ergebnis im Chat ist der fertige Entwurf, nicht ein Bericht darüber.

## Entscheidungen (mit Nik abgestimmt)

| Frage | Entscheidung |
|---|---|
| URL-Budget | 4.096 Zeichen — volle Karte passt (3.372 gemessen), klar unter der 8-KB-Request-Line von nginx/CDNs |
| Verhalten im Chat | nur Entwurf: `draft_application` sofort, keine Bullets, keine Rückfrage davor |
| Zusatzfelder | DB-Zeile, Google-Impressum-Link, Onsite-Warnung + Schwelle, absolute Postzeit (Berlin) — plus die gesetzten LinkedIn-Links (Firma, Person) |
| Folgeänderung | `get_listing` gibt die `raw`-Fakten mit zurück |

## Build-Steps

- [x] 1. **MatchMessage trägt die fehlenden Felder** — `source`, `origin`,
  `status`, `threshold`, `posted_at_label` (Berlin), gesetzt in
  `to_match_message`/`from_stored`; `BERLIN` wird aus `normalize.py` öffentlich.
  Schwelle kommt aus `CheckService._threshold` bzw. `settings.match_threshold`.
- [x] 2. **Prompt neu gebaut** (`claude_link.py`) — Kontextblock (DB, Schwelle,
  Postzeit, Onsite-Warnung, LinkedIn Firma/Person, Impressum-Suche) und
  Auftragsblock (sofort `draft_application`, Tool-Inventar, Ausgaberegeln,
  Rückfragen als nummerierte Liste mit `(empfohlen)`); eigener Auftrag für das
  ungespeicherte Listing; `MAX_URL_CHARS = 4_096`; die Kurzform verliert nur
  noch die Fakten, nicht den Auftrag.
- [x] 3. **`get_listing` gibt die raw-Fakten zurück** (`mcp_server.py`) —
  company, contact_name, client_type, remote, contract, workload, duration,
  start, posted_at_berlin, apply_by, industry, language, onsite_only, über
  `from_stored` statt einer zweiten Parse-Stelle.
- [x] 4. **Tests** — `test_claude_link.py` neu (14 Fälle, inkl. "die volle Karte
  ist die, die ausgeliefert wird"), Builder-Tests in `test_messages.py`,
  raw-Fakten in `test_mcp_server.py`.
- [x] 5. **Doku** — `docs/claude-setup.md` (Aufbau des Prompts, neues Budget,
  Troubleshooting-Zeile), `README.md`, `project-overview.md`.

## Done when

- [x] Eine voll gefüllte Karte wird **mit** Fakten ausgeliefert (Test prüft
  Gleichheit mit `_build(..., with_card=True)`, nicht nur die Länge).
- [x] Der Prompt nennt jedes `project_pilot_*`-Tool und markiert
  `send_application` als "nur nach ausdrücklichem OK".
- [x] Firma und Ansprechpartner ergeben zwei getrennte LinkedIn-Suchen; fehlen
  sie, entsteht keine leere Suche.
- [x] Quality-Gate grün: `ruff check`, `ruff format --check`, `mypy`, `pytest`
  (505 passed).
- [ ] **Offen, nur von Nik prüfbar:** einmal `uv run project-pilot test-match`
  und auf dem Handy Bewerben tippen — trägt Telegram die ~3,4k-Zeichen-URL und
  füllt `claude.ai/new` den Composer? Wenn nicht: `MAX_URL_CHARS` zurück auf
  2.048, dann fällt der Link automatisch auf die Kurzform zurück.
