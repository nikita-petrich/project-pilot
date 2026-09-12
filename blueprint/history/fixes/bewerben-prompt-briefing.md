# Fix: Bewerben-Prompt wird Briefing statt Kurzbrief

> Ad-hoc-Änderung, gebaut auf `claude/dreamy-hamilton-eqq3wk` (2026-09-12).
> Zwei Teile: der Prompt selbst (PR #42), dann der LLM-Pfad, den der Gerätetest
> aufgedeckt hat.

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
  (513 passed).
- [x] **Am Gerät geprüft** (Listing 447): Telegram trug den Link, der Composer
  ging gefüllt auf, Kontextblock und beide LinkedIn-Suchen waren da, und bei
  einem Tool-Fehler kam die nummerierte Liste mit `(empfohlen)` statt Fließtext.
- [ ] **Offen:** die Karte selbst fiel dabei weg — 447 braucht mit Karte 4.342
  Zeichen, das Limit steht auf 4.096. Echte LLM-Urteile sind zwei- bis dreimal
  so lang wie das synthetische Beispiel, an dem das Budget bemessen wurde.
  Nächster Schritt ist nicht „Zahl erhöhen", sondern Niks Vorschlag: kurzer
  Prompt (Headline + Link + `listing_id`) und ein Skill, der die Karte per
  `get_listing` im Chat rendert. Eigener Durchgang.

---

## Nachtrag: LLM-Pfad reparieren (gleicher Branch)

Beim Gerätetest lieferte `draft_application` `schema violation (empty parse)`.
Ursache: bei Sonnet 5 (und Opus 5) ist adaptives Thinking an, wenn `thinking`
weggelassen wird — und Thinking-Tokens zählen gegen `max_tokens`. Mit 8.192 war
das Anschreiben mitten im `body` abgeschnitten, `parsed_output` also `None`.
Der Adapter warf `stop_reason` weg, deshalb log der Fehler die falsche Fährte.

- [x] 6. **Token-Decken hoch** — `DRAFT_MAX_TOKENS` und `VERDICT_MAX_TOKENS`
  von 8.192 auf 16.000 (Anthropics eigener Richtwert für nicht-gestreamte Calls).
- [x] 7. **`stop_reason` durchreichen** — `LlmResponse`/`DraftResponse` tragen
  ihn, `parse_failure()` macht daraus „response truncated at max_tokens" statt
  „schema violation". Das war der eigentliche Defekt: ein blinder Fehler.
- [x] 8. **`LLM_EFFORT` als ENV** (Anthropic, `output_config.effort`) — nicht
  hart im Adapter, weil `LLM_MODEL` frei konfigurierbar ist und `effort` bei
  Sonnet 4.5 / Haiku 4.5 einen Fehler wirft. Leer = Parameter wird nicht
  gesendet. Typ ist ein `Literal`, damit mypy und pydantic dieselbe Menge sehen.
- [x] 9. **Token-Leck geschlossen** — `httpx`/`httpx2`/`httpcore` auf WARNING.
  Das Telegram-Bot-Token steht in der Request-URL, und httpx loggt die auf INFO;
  es lag damit in jedem `docker compose logs`.
- [x] 10. **Doku** — `.env.example`, `README.md`, `docs/deployment.md`.

### Von Nik zu tun (nicht im Code)

- `LLM_MODEL=claude-opus-5` und `LLM_EFFORT=low` im `prod`-Environment setzen;
  `render-env.py` nimmt jede Variable automatisch mit, kein Workflow-Change.
- Bot-Token bei BotFather neu ausstellen — das alte stand im Klartext im Log.
- `uv run pytest -m eval` nach dem Modellwechsel: der Prompt ist pro Modell
  getunt, der Golden Set sagt, ob die Urteile halten.

## Verified

`uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
`uv run pytest` (517 passed, lokale Postgres).

Am Gerät (Listing 447, Score 85): Telegram trug den Link, der Composer ging
gefüllt auf, DB-Zeile, Schwelle, Eingestellt-Zeitpunkt und beide LinkedIn-Suchen
standen drin, und auf den Tool-Fehler antwortete der Chat mit der nummerierten
Optionsliste samt `(empfohlen)` statt mit Fließtext.

## Was offen bleibt

Die Kartenfakten fielen beim Gerätetest aus dem Prompt: Listing 447 braucht mit
Karte 4.342 Zeichen, das Limit steht auf 4.096. Echte LLM-Urteile sind zwei- bis
dreimal so lang wie das synthetische Beispiel, an dem das Budget bemessen wurde.

Der nächste Schritt ist **nicht** eine größere Zahl, sondern Niks Vorschlag: der
Prompt schrumpft auf Headline, Listing-Link und `listing_id`, und ein Skill zieht
die Karte per `project_pilot_get_listing` und rendert sie im Chat. Dann gibt es
kein URL-Budget mehr, das reißen kann — und die raw-Fakten, die `get_listing`
seit diesem Fix mitliefert, sind genau die Vorarbeit dafür. Eigener `/fix`.
