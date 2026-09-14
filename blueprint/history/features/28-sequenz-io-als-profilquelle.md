# 28 · sequenz.io als Profilquelle

Das Profil wird nicht mehr im Repo gepflegt. Es ist das der Website, zur Laufzeit
geholt — denn ein Profil, das an zwei Stellen gepflegt wird, ist an einer davon
falsch, und es ist immer die Kopie, die niemand ansieht.

## Homepage-Repo (`feature/profile-feed`)

- `lib/profile-feed.ts` erzeugt die Markdown-Zwillinge aus `getContent(locale)` —
  nicht handgeschrieben, sonst wäre die Dopplung nur umgezogen. Routen `/de.md`
  und `/en.md`, statisch vorgerendert (45 KB bzw. 44 KB).
- `/llms.txt` als Wegweiser nach dem aihero.dev-Muster.
- `/api/profile.json` trägt die Felder, die Prosa nicht genau genug trägt:
  `available_from`, `capacity_percent`, `onsite_max_percent`, Stunden- und
  Tagessatz, Buchungslinks je Sprache, Plattform-Profile und zwei fertige Sätze
  (Verfügbarkeit, Stundensatz) je Sprache.
- Eckdaten öffentlich: Stundensatz „ab 80 €/h · ab 640 €/Tag · projektabhängig"
  statt „auf Anfrage"; Verfügbarkeit stand bereits auf „ab sofort".
- Telefon, E-Mail, USt-IdNr. und der Signatur-Ort liegen jetzt in `lib/profile.ts`,
  damit Impressum, Sidebar und Feed nicht auseinanderlaufen.

## project-pilot

- `profile_source.py`: holt Zwilling + Feed, validiert den Feed über Pydantic,
  retryt eine flakige Edge dreimal — und wirft sonst `ProfileUnavailableError`.
- `profile_loader.py`: `profile.text` = geholte Hälfte + gerenderte Blöcke
  („Availability & terms", „Contact & Signature") + private Hälfte.
  `profile_hash` hasht das Ergebnis, ist also weiter ein Inhalts-Hash.
- `profile/private.yaml` ersetzt `profile.md` + `constraints.yaml`: No-go-Branchen
  und -Technologien plus die deterministischen Regeln. Die Begriffslisten bleiben
  exakte Listen (`java` feuert nicht auf „JavaScript", `spring` fängt „Spring Boot").
- `profile_snapshots` (Migration `b8e3f47c1d92`): jeder neue Profilstand wird einmal
  unter seinem Hash abgelegt — beim Lauf und bei jedem Entwurf —, damit ein
  gespeichertes Urteil später noch sagen kann, wogegen es bewertet wurde.
- Fehlgeschlagener Abruf: harter Abbruch plus Telegram-Betriebswarnung. Kein
  stiller Rückfall auf einen alten Stand.
- `ENRICHMENT_ENABLED` ist ersatzlos weg: die Bewerbungsstrecke braucht einen
  Empfänger, und genau diesen Handgriff nimmt die Recherche ab.
- `application.md` liest jetzt Abschnitte, die es wirklich gibt
  („Availability & terms", „Languages") und zitiert die fertigen Sätze aus dem Feed.

## Dazu gekommen beim Bauen

- **Der Zwilling brachte seinen eigenen Kontaktblock mit.** Der Parser blieb am
  ersten „Contact & signature" hängen — dem der Website — und lieferte die halbe
  Signatur: kein Name, keine Buchungslinks, kein LinkedIn. Gefunden erst beim
  echten Durchlauf gegen den lokal gestarteten Build, nicht von den Unit-Tests.
  Zwei Korrekturen: die Website nennt ihren Abschnitt „Kontakt"/„Contact", und der
  Parser nimmt den **letzten** Treffer, damit nie die Website entscheidet, was in
  einer ausgehenden Signatur steht. Regressionstest liegt bei.
- Das Markdown-Entkleiden im Signatur-Parser wurde damit unerreichbar und ist raus.

## Verified

- `ruff check`, `ruff format --check`, `mypy --strict`, `pytest` — 567 grün,
  inklusive der DB-Tests gegen ein echtes Postgres.
- Beide Migrationen von `base` auf eine frische Datenbank angewandt.
- Website gebaut (`pnpm build`), alle vier Routen statisch vorgerendert.
- End-to-End: den Build lokal gestartet und das Profil damit geladen — jeder
  Signaturschlüssel löst auf, die No-gos sind im Text, der Hash ist stabil.

## Offen

- Kein LLM-Key in dieser Umgebung: dass Stundensatz und Verfügbarkeitssatz
  tatsächlich im erzeugten Anschreiben landen, ist durch den Prompt und den
  Kontrakt-Test abgesichert, aber nicht an einem echten Entwurf gesehen.
- Der Zwilling ist mit 45 KB gut doppelt so groß wie das alte `profile.md` (21 KB).
  Bessere Matching-Grundlage, aber auch mehr Tokens je Bewertung — beobachten.
