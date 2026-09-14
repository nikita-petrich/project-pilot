# Learnings

> Muster aus echten Läufen, damit derselbe Fall beim nächsten Mal sauber behandelt
> wird. Jeder Eintrag nennt, **was passiert ist**, **die Regel** und **wo sie
> durchgesetzt wird**. Die Regel selbst lebt im Code, im Prompt oder im Skill — diese
> Datei erklärt nur, warum. Neuer Fall → neuer Eintrag, fortlaufend nummeriert.

## Kontakt und Empfänger

### L1 · Ansprechperson im Inserat ≠ Inhaber der gefundenen Adresse

- **Gesehen:** 14.09.2026, Listing 1131 (Randstad Digital). Inserat nennt
  *Talissa Blajan*; die einzige auffindbare Adresse steht auf der Firmenseite und
  gehört *Laura Roßmeier*. Der Chat meldete das als Problem und schlug vor, die
  Anrede auf Laura zu ändern.
- **Regel:** Das ist bei Vermittlern der Normalfall, kein Fehler. Die **Anrede geht an
  die Ansprechperson aus dem Inserat**, **verschickt wird an die beste gefundene
  Adresse** — die Vermittlerfirma leitet intern an die Ansprechperson weiter. Kein
  offener Punkt, kein Vorschlag, die Anrede umzustellen. Die Empfängerzeile sagt beides.
- **Umgesetzt:** Anrede aus dem Inserat — `application.md` (Anrede-Regel, Block
  `## Ansprechpartner`). Empfängerwahl und Hinweis — Skill `write-application`
  (Account- und Repo-Variante).

### L2 · Firmenseite ohne Kontakt-Links

- **Gesehen:** 14.09.2026, Randstad-Firmenseite auf freelancermap. Keine `mailto:`,
  keine `tel:`, kein Website-Link — Telefon, E-Mail und Website standen nur im
  eingebetteten JSON-Datensatz. Die Seite war 3,1 MB groß (Übersetzungstabelle) und
  wurde deshalb komplett verworfen.
- **Regel:** Firmenseiten werden bis zur Byte-Grenze gelesen statt verworfen, und der
  eingebettete Datensatz zählt als „vom Anbieter angegeben" wie die Links. Ein Wert
  zählt nur, wenn er die Form hat, die er behauptet (Adresse, Nummer, Domain) —
  dieselben Schlüssel beschriften auch Formularfelder.
- **Umgesetzt:** `enrichment/fetch.py` (`_read_capped`), `enrichment/extract.py`
  (`embedded_contacts`, `embedded_site`), `enrichment/service.py` (`_board_contacts`).

### L3 · Namen mit doppeltem Leerzeichen

- **Gesehen:** 14.09.2026, `firstName` = "Talissa " → „Talissa␣␣Blajan" in Karte,
  Anrede und LinkedIn-Suche.
- **Regel:** Namen werden aus ihren Teilen mit genau einem Leerzeichen zusammengesetzt.
- **Umgesetzt:** `ingestion/normalize.py` (`join_name`).

## Anschreiben und Nachrichten

### L4 · Ein Register — immer „Sie"

- **Gesehen:** 14.09.2026, Verbindungsnachricht „Hallo Talissa, … Ihr Projekt … mit Ihnen".
- **Regel:** Formell durchgehend. Ohne bekanntes Herr/Frau ist „Guten Tag Vorname
  Nachname," die formelle Form — nie aus dem Vornamen raten, nie „Hallo Vorname".
- **Umgesetzt:** `application.md` (Siez-Regel), `enrichment/message.py`.

### L5 · Was das Modell nur meistens richtig macht, wird nachträglich korrigiert

- **Gesehen:** 14.09.2026. Derselbe Prompt lieferte einmal „ich bin …" nach der Anrede,
  beim nächsten Listing „Ich bin …"; der Vertraulichkeitshinweis kam einmal als
  Absatz, einmal mit harten Zeilenumbrüchen.
- **Regel:** Alles, was mechanisch prüfbar ist, wird nach der Generierung
  deterministisch korrigiert und getestet — nicht einer weiteren Prompt-Zeile
  überlassen. Nur eindeutige Fälle: das Pronomen „Ich", der exakte Hinweistext.
- **Umgesetzt:** `application/letter.py`, angewendet in `application/schemas.py`.

### L6 · Beim Überarbeiten landete die LinkedIn-Notiz im Mailtext

- **Gesehen:** 14.09.2026, Überarbeitung von Entwurf 46: „LinkedIn: Guten Tag …" stand
  unter dem Vertraulichkeitshinweis, also in der E-Mail.
- **Regel:** Felder gehen getrennt ans Modell, und nach dem Vertraulichkeitshinweis —
  dem letzten Block — wird nichts übernommen.
- **Umgesetzt:** `application/generator.py` (`revise`), `application/letter.py`
  (`end_at_confidentiality_notice`).

### L7 · CVs immer frisch aus Google Drive — sonst kein Versand

- **Gesehen:** 14.09.2026. Nach einem Deploy war der CV-Cache im Container leer; die
  Vorschau meldete „CV fehlt". Beim Versand wäre eine Mail bei Drive-Ausfall ohne CV
  rausgegangen, obwohl der Text die Anhänge ankündigt.
- **Regel:** Beim **Versand** müssen beide CVs in diesem Moment aus Drive geladen
  werden. Klappt das nicht: Fehler, **keine Mail**, Entwurf bleibt versandbereit für
  einen neuen Versuch. Eine ältere Kopie im Cache zählt nicht. Vorschau und
  Überarbeitung holen die CVs, wenn sie fehlen.
- **Umgesetzt:** `application/cv_drive.py` (`refresh` meldet, was geladen wurde),
  `application/service.py` (`_require_fresh_cvs`, `_ensure_cvs_cached`).

## Betrieb

### L8 · Ein grüner Deploy ist kein Beweis

- **Gesehen:** 14.09.2026. Der Worker crashte bei jedem Start (113 Neustarts), der
  Deploy meldete trotzdem „healthy" — der Healthcheck zählte den letzten Lauf des
  alten Images mit.
- **Regel:** Ein Deploy ist erst gesund, wenn der neue Container ohne Neustart über
  mehrere Prüfungen stabil läuft. Und nach jedem Deploy einmal selbst schauen:
  `docker compose ps` plus Restart-Zähler.
- **Umgesetzt:** `deploy/remote-deploy.sh`; Regressionstest für den Crash selbst in
  `tests/test_cli.py`.

### L9 · Betriebswarnungen einmal, nicht bei jedem Neustart

- **Gesehen:** 14.09.2026. Bei nicht erreichbarer Website hätte jeder Container-Neustart
  dieselbe Telegram-Warnung geschickt.
- **Regel:** Dauerprozesse stürzen bei einem Ausfall nicht ab, sondern warten: eine
  Warnung, eine Entwarnung. Handbefehle melden Fehler nur im Terminal.
- **Umgesetzt:** `cli.py` (`_wait_for_profile`).
