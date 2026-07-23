# Current Feature

> **Fix spec.** Ad-hoc fix, built on the current branch via Autopilot.

## Fix: Parser gegen die reale freelancermap-Seite (react-on-rails JSON)

### Problem

`uv run project-pilot run-once` bricht mit `SelectorMismatchError: no list cards
matched 'article.project-card'` ab. Die Selektoren in
`src/project_pilot/ingestion/parser.py` wurden gegen **synthetische** Fixtures
gebaut (freelancermap war beim Compliance-Check geblockt) und passen nicht zur
echten Seite.

### Verifizierte Realität (live inspiziert, HTML lokal in scratchpad gespeichert)

- **Listenseite** ist server-gerendert, enthält aber die Projektdaten strukturiert
  in einem `<script class="js-react-on-rails-component" data-component-name="ProjectSearch">`
  JSON-Blob: `initialResults[]` (je `id, slug, title, created` (ISO min, +TZ),
  `city`, `country`), `currentPage`. **Keine** server-gerenderten Pagination-`<a>`
  (die `pagenr`-Links liegen nur im JSON) → Pagination per `pagenr`-Inkrement.
- **Detailseite**: Daten in `<script ... data-component-name="ProjectShow">` unter
  `project.*`: `title`, `created` (ISO min), `startYear/startMonth/startText`,
  `contractType.remoteInPercent`, `city`, `country.nameDe`, `description` (HTML),
  `skills.enabled[].localizedName`, `durationInMonths`.
- Detail-URL wird aus `slug` gebaut: `/projekt/{slug}`.
- Klärt Feature-1-Offenpunkte: kein JS-Rendering nötig (Daten sind SSR-JSON);
  `posted_at` ist minutengenau.

### Ansatz

Parser durchgängig **JSON-Blob-basiert** statt CSS. Ein Extractor findet den
Blob per `data-component-name`, `json.loads`, sonst `SelectorMismatchError`
(laut statt still). Losslessness: der rohe `project`-Dict wandert in `raw`.

### Done when

- `parse_list_page` liest `ProjectSearch.initialResults` → `ListingSummary[]`
  (URL aus slug, Titel, `posted_at` minutengenau). Leere Ergebnisse → `[]`;
  fehlender Blob → `SelectorMismatchError`.
- Pagination per `next_page_url` (inkrementiert `pagenr`); Pipeline stoppt bei
  leerer Seite, Watermark/known-hash, oder `MAX_LIST_PAGES`.
- `parse_detail_page` liest `ProjectShow.project` → `ParsedListing` (Titel,
  posted minute, Skills, Start via year/month/text, Remote via Prozent, Ort aus
  city+country, Description als Text, `raw=project`).
- Fixtures spiegeln die **reale** Blob-Struktur (sanitisiert, keine echten
  Personendaten); alte synthetische CSS-Fixtures ersetzt.
- Gate grün: `ruff check`, `ruff format --check`, `mypy`, `pytest` (Kernmodule ≥90%).
- Nachweis: der neue Parser parst die **real gecrawlten** HTML-Samples korrekt.

### Build steps

- [x] **1. Normalisierungs-Helfer** in `normalize.py` (+ `test_normalize.py`):
  `remote_status_from_percent(pct|None)` (100→remote, 0→onsite, 1–99→hybrid,
  None→unknown), `start_from_parts(year, month, text)` (sofort/asap→(None,True),
  keine-angabe/leer→(None,False), sonst `date(year,month,1)`), `html_to_text(html)`,
  `next_page_url(url)` (setzt/inkrementiert `pagenr`).
- [ ] **2. Blob-Extractor + List-Parser**: `_react_component(html, name) -> dict`
  (raise `SelectorMismatchError` bei fehlend/kein-JSON). `parse_list_page` liest
  `ProjectSearch.initialResults` → Summaries (leer→`[]`). `parse_next_page_url`
  entfernt. Neue Fixture `freelancermap_list.html` (ProjectSearch-Blob, 3 Items,
  darunter minutengenau + Rand-URL). List-Tests in `test_parser.py` anpassen.
- [ ] **3. Detail-Parser**: `parse_detail_page` liest `ProjectShow.project` →
  `ParsedListing` (Mapping s.o.; `end_date=None`, `durationInMonths` in `raw`).
  Neue Fixtures `freelancermap_detail_asap_remote.html` (startText "ab sofort",
  100 % remote) und `freelancermap_detail_dated_onsite.html` (startYear/Month,
  0 % → onsite). Detail-Tests anpassen. `SelectorMismatchError` bei fehlendem Blob.
- [ ] **4. Pipeline-Pagination**: `_collect_new_summaries` nutzt `next_page_url`
  (Inkrement) statt Link-Following; Stop bei leerer Seite. `test_pipeline.py`
  Routing/Fixtures anpassen (Detail-URLs aus slug; leere Seite-2 beendet Loop).
- [ ] **5. Acceptance**: Gate grün; neuen Parser gegen die real gecrawlten
  Samples laufen lassen und die extrahierten Felder prüfen.

### Out of scope (bewusst)

- `end_date`-Ableitung aus `durationInMonths` (Quelle liefert Dauer, kein
  Enddatum) — `raw` behält den Wert; spätere Ableitung möglich.
- Parsen der ganzen Ausschreibung aus `initialResults` (Detail-Fetch bleibt, da
  Skills/Start/Remote nur im Detail-Blob stehen).
- README/Fixtures-Doku-Feinschliff (README-Notiz kurz aktualisieren).
