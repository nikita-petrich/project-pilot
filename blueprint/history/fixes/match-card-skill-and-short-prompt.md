# Fix: die Karte kommt aus dem Chat, nicht aus der URL

> Ad-hoc-Änderung, gebaut auf `claude/dreamy-hamilton-eqq3wk` (2026-09-12/13).
> Direkte Fortsetzung von `bewerben-prompt-briefing.md`, das genau dieses
> Problem als offen hinterlassen hat.

## Problem

Der Bewerben-Prompt trug die volle Übersichtskarte in der URL. Damit war die
URL-Länge die bindende Grenze: Listing 447 maß 4.093 Zeichen bei einem Limit von
4.096 — drei Zeichen Luft. Dass die Fakten überlebten, lag daran, dass Opus 5 bei
`effort: low` zufällig ein kompaktes Urteil schrieb; das gespeicherte Sonnet-Urteil
desselben Listings ergab 4.342 und hätte alles verdrängt. Jede Lösung in dieser
Richtung war eine größere Zahl.

## Lösung

Die Karte kommt von dort, wo sie gerendert wird.

- Neues MCP-Tool `project_pilot_match_card(listing_id)` gibt den fertigen Text
  zurück — Headline, alle Fakten, gespeichertes Urteil, DB-Koordinaten, Schwelle,
  Einstellzeitpunkt, Recherche-Links.
- Neuer Skill `match-card` druckt ihn wortwörtlich, ohne eigenes Layout.
- `render_card` und `research_lines` wandern nach `notification/messages.py`,
  neben den Renderer, den sie erweitern. **Ein Layout für drei Oberflächen** —
  Telegram-Karte, Chat und Skill können nicht auseinanderdriften.
- Der Prompt trägt nur noch, was ein Chat nicht nachschlagen kann: Headline
  (davon nimmt der Chat seinen Titel), Listing-Link, DB-Koordinaten und den
  Auftrag, beide Skills nacheinander laufen zu lassen.

**1.088 statt 4.093 Zeichen** für dasselbe Listing — und die Länge eines
LLM-Urteils entscheidet nicht mehr darüber, was Nik zu sehen bekommt. Ein
ungespeichertes `test-match`-Listing trägt seine Karte weiterhin inline, weil es
keine Zeile zum Nachladen gibt.

## Dazu, auf Niks Wunsch

- Fehlender Ansprechpartner erscheint als `🙋 LinkedIn Person: K.A.` statt als
  wegfallende Zeile — eine Anzeige ohne Kontakt ist selbst eine Information.
- Die Regeln verlangen jetzt, Folge-Aktionen **in Worten** zu beschreiben statt
  mit Funktionsnamen. Das war gemeint mit „weniger robotisch": es ging um die
  Antwort des LLM, nicht um den Prompt.
- `LINKEDIN_LIMIT` 300 → 200.

## Bekannte Folge des 200er-Limits

Buchungslink und Telefonnummer belegen zusammen 143 der 200 Zeichen. Für Anrede
und Projektbezug bleiben 56; „Guten Tag Herr Koch, zu Ihrer
AI-Developer-Ausschreibung." braucht 57. In der Praxis kürzt
`fit_linkedin_message` deshalb fast immer die Anrede:

```
Guten Tag Herr Koch, zu Ihrer… Kostenloses Erstgespräch: <Link> — oder rufen Sie
mich direkt an: +49 1567 9088678.
```

Link und Telefonnummer bleiben dabei immer intakt (der Fitter wirft die Mitte
weg, nie das Ende). Nik wurden drei Varianten mit fertigen Beispielnachrichten
vorgelegt — Telefonnummer streichen (197 Zeichen, alles passt), CTA-Wortlaut
kürzen (167 Zeichen, ohne Passt-Halbsatz) oder so lassen. Entscheidung: so
lassen. Ein Test pinnt die Rechnung, damit eine spätere Änderung ihren Preis zeigt.

## Verified

`uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
`uv run pytest` (521 passed, lokale Postgres).

## Offen, außerhalb des Codes

Der `match-card`-Skill muss in Niks Claude-Account angelegt werden. Der Chat
zieht seine Skills aus dem Account, nicht aus dem Repository — ohne das findet
der Bewerben-Klick den Skill nicht und zeigt keine Karte.
