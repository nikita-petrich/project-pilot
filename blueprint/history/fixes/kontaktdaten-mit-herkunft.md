# Kontaktdaten mit Herkunft — die freelancermap-Firmenseite als erste Quelle

**Type:** Fix

## Problem

Drei Lücken in der Kontakt-Anreicherung, alle im selben Ablauf:

1. **Eine Quelle, die wir schon haben, wird nicht genutzt.** In `listings.raw` steht
   `companyUrl` — die freelancermap-Firmenseite des Vermittlers:

   ```json
   "companyUrl": "/firma/556-weissenberg-business-consulting-gmbh-weissenberg-delivering-excellence"
   ```

   Diese Seite zeigt Telefon, E-Mail und Website öffentlich (im Inkognito-Fenster
   verifiziert, kein Login). `enrich_company` ignoriert sie und sucht stattdessen
   blind im Web nach der Firmenwebsite.

2. **Die Antwort sagt nicht, woher ein Datum kommt.** `ContactEnrichment.emails`
   und `.phones` sind nackte `list[str]`. Eine Adresse, die der Vermittler selbst
   auf freelancermap hinterlegt hat, steht gleichwertig neben einer, die die
   eigene Impressum-Suche irgendwo aufgelesen hat. Für die Entscheidung „schicke
   ich da wirklich hin" ist das der wichtigste Unterschied.

3. **Es gibt keine definierte Kette, wenn kein Ansprechpartner genannt ist.**
   Die Anzeige trägt `firstName`/`lastName` — oder eben nicht. Heute rät der Chat,
   was dann gilt.

## Der Fix

Die Firmenseite wird zu einer eigenen, bevorzugten Quelle, und jedes Datum trägt
seine Herkunft mit.

### 1. Die URL wird gelesen, nie gebaut

`raw["companyUrl"]` ist ein absoluter Pfad relativ zu `https://www.freelancermap.de`.
Er wird **so übernommen**. Eine URL aus `id` + Firmenname zusammenzusetzen ist
verboten: der Slug kann sich ändern, und wir bauen stillschweigend eine tote URL.
Fehlt `companyUrl`, entfällt diese Quelle — kein Ersatzverfahren.

### 2. Herkunft pro Datum

```python
@dataclass(frozen=True, slots=True)
class ContactDatum:
    value: str
    source: Literal["freelancermap", "web"]
```

`ContactEnrichment.emails` / `.phones` / `.persons` tragen `ContactDatum` statt
`str`. `best_email` / `best_phone` bleiben in der Bedeutung, bevorzugen aber
`freelancermap` vor `web`.

`contact_leads.emails` / `.phones` / `.persons` sind JSONB — die Formänderung
braucht **keine Alembic-Migration**. Beim Lesen werden alte Zeilen (reine Strings)
als `source="web"` interpretiert.

### 3. Die Kette, einmal für E-Mail und einmal für Telefon

| Ausgangslage | Reihenfolge |
|---|---|
| Anzeige nennt einen Ansprechpartner (`firstName`/`lastName`) | Firmenseite → personenbezogene Adresse der eigenen Suche → Impressum |
| Anzeige nennt niemanden | Firmenseite → **direkt** Impressum, keine Personensuche |

Identisch für die Telefonnummer. Trägt die Firmenseite ein Datum, gilt es als
gefunden und die weitere Suche dafür entfällt — sie ist die genauere Quelle,
weil der Vermittler sie selbst gepflegt hat.

### 4. Die MCP-Antworten tragen es nach außen

- `_enrichment_payload`: `emails`/`phones`/`persons` als Objekte mit `value` und
  `source`; neues Feld `company_page` (die Firmenseiten-URL).
- `_listing_facts`: `company_page` ergänzen, damit `get_listing` und `match_card`
  die Seite zeigen, ohne dass jemand anreichern muss.

## Was nicht kaputtgehen darf

- Anreicherung bleibt opt-in (`ENRICHMENT_ENABLED`); ohne das Flag ändert sich nichts.
- Das robots-Gate und die 2–5-Sekunden-Politeness gelten für die Firmenseite genau
  wie für jede andere Seite. Ein 403 wird **nicht** wiederholt.
- LinkedIn wird weiterhin nie abgerufen, nur als Suchlink angeboten.
- `contact_leads` bleibt append-only.
- Ein Fehlschlag beim Holen der Firmenseite bricht die Anreicherung nicht ab —
  es fehlt dann nur diese Quelle.

## Build-Schritte

### [x] Schritt 1 — `ContactDatum` und die Herkunft durch die Schicht ziehen

`enrichment/schemas.py` bekommt `ContactDatum`; `ContactEnrichment` und
`EnrichmentService.enrich` liefern es; `render.py`, `cli.py` und
`_enrichment_payload` zeigen es an. Alle heutigen Funde bekommen `source="web"`.

**Done when:** `uv run project-pilot enrich "<Firma>"` zeigt hinter jeder Adresse
und Nummer ihre Herkunft, und `contact_leads` speichert beide Formen lesbar.

### [x] Schritt 2 — die Firmenseite als Quelle

Neue Funktion in `enrichment/listing.py`: `company_page_url(listing)` liest
`raw["companyUrl"]` und macht sie absolut. `EnrichmentService.enrich` bekommt
einen Parameter `company_page: str | None` und holt diese Seite als erste, über
denselben Fetcher wie alles andere; die Treffer werden mit
`source="freelancermap"` markiert.

**Done when:** `uv run project-pilot enrich --listing-id 447` zeigt
`i.strucken@weissenberg-solutions.de` und `0211 54080932` mit Herkunft
`freelancermap`, gegen eine Fixture der Firmenseite.

### [x] Schritt 3 — die Fallback-Kette

Die Reihenfolge aus der Tabelle oben als eigene, testbare Funktion, getrennt vom
Holen der Seiten. Ob die Anzeige einen Ansprechpartner nennt, kommt aus
`raw["firstName"]`/`raw["lastName"]` über das bestehende `resolve_contact_name`.

**Done when:** Tests decken beide Zeilen der Tabelle ab — mit und ohne
Ansprechpartner, und je einmal mit und ohne Treffer auf der Firmenseite.

### [x] Schritt 4 — MCP-Antworten und Skill

`company_page` in `_listing_facts` und `_enrichment_payload`; der
`enrich-company`-Skill beschreibt, wie die Herkunft zu lesen ist und dass eine
`web`-Adresse vor dem Senden zu prüfen ist.

**Done when:** `project_pilot_match_card` zeigt die Firmenseite, und
`project_pilot_enrich_company` liefert jede Adresse mit ihrer Herkunft.

## Verify

1. `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`
2. `uv run project-pilot enrich --listing-id 447` — Telefon und E-Mail des
   Vermittlers erscheinen mit Herkunft `freelancermap`, die Impressum-Funde mit `web`
3. Ein Listing ohne `companyUrl`: die Anreicherung läuft wie bisher durch, nichts
   bricht
4. Ein Listing ohne Ansprechpartner: die Personensuche entfällt, die
   Impressum-Adresse kommt direkt

## Dazu gekommen, beim Bauen

- **`company_page` wird nicht gespeichert.** Ursprünglich als Spalte auf
  `contact_leads` geplant; es ist eine reine Funktion von `listings.raw`, das
  ohnehin verlustfrei gehalten wird — eine Migration für redundante Daten.
- **`company_page_url` sitzt in `ingestion/normalize.py`**, nicht in
  `enrichment/listing.py`: so kann die Notification-Schicht die Firmenseite auf
  die Karte schreiben, ohne sich das datenbankbewusste Enrichment-Modul
  einzuhandeln. Gejoint wird gegen `listing.external_url`, damit die Funktion
  boardneutral bleibt.
- **Nur die Firmenseite gilt als „genannt".** Eine Adresse aus dem Anzeigentext
  (`known_email`) oder ein übergebener `person` bleibt `web`: beides ist
  plausibel, nicht veröffentlicht. Für ein Werkzeug, das echte Mails verschickt,
  ist „nochmal ansehen" die sichere Richtung.
- **Telefon-Dedupe repariert** (außerhalb der Spec, aber von ihr aufgedeckt):
  `+49 211 54080932` und `0211 54080932` waren zwei Einträge, weil der
  Dedupe-Schlüssel Amtsnull und Ländervorwahl auseinanderhielt. Jetzt wird auf
  den Teilnehmer-Schwanz dedupliziert.

## Verified

`uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
`uv run pytest` (546 passed, lokale Postgres).

Ende-zu-Ende durch den echten CLI-Ausgabepfad, gegen
`tests/fixtures/freelancermap_company_page.html`:

```
Company page: …/firma/556-weissenberg-business-consulting-gmbh-…
Website:      https://www.weissenberg-solutions.de/
E-mails: i.strucken@weissenberg-solutions.de (freelancermap), info@… (web)
Phones:  +4921154080932 (freelancermap), +4921154080000 (web)

search queries made: []          ← die Firmenseite hat die Websuche erspart
```

## Offen

- Der Lauf gegen die **echte** Datenbank (`enrich --listing-id 447`) steht aus:
  freelancermap ist aus der Build-Umgebung per Egress-Policy gesperrt und die
  Produktions-DB liegt auf dem VPS. Derselbe Codepfad, aber kein echter Abruf.
- `outbound_site` nimmt den ersten externen Link, der kein Verzeichnis und kein
  soziales Netz ist. Auf einer echten Seite könnte ein Cookie-Banner-Link
  zuerst kommen; sichtbar bleibt es, weil die Herkunft mitläuft.
- Vorbestehende Altlast, nicht angefasst: `extract_persons` greift bei
  „Ansprechpartner: Ines Strucken" plus „LinkedIn" im Footer einen zu langen
  Namen. Nur Anzeige.
