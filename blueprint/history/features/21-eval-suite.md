# Current Feature

> **Generated file.** Holds the one feature or fix being built right now.

# Feature: Eval-Suite

**From build-plan:** feature 21
**Status:** built

## Goal

Messbar machen, ob Änderungen an Urteilslogik (Prompt, Skills, Modellwechsel) die
Match-Qualität verbessern oder verschlechtern: ein Golden-Set gelabelter
Listings, ein pytest-Eval-Runner gegen den echten `LlmMatcher`, und ein CI-Job,
der bei vorhandenem `OPENAI_API_KEY` läuft und unter der Baseline rot wird.

## In scope

- `tests/eval/golden.jsonl` — Start-Golden-Set (~12 Fälle: klare Matches, klare
  No-gos je Kategorie, Grenzfälle wie Frontend-gegen-Java-Backend und
  Versions-Regel). **Labels sind provisorisch von der KI gesetzt und von Nik zu
  reviewen** (offener Punkt).
- `tests/eval/test_golden.py` — `@pytest.mark.eval`, skippt ohne
  `OPENAI_API_KEY`; fährt jeden Fall durch `LlmMatcher` (echter Prompt, echter
  No-go-Post-Check) und asserted Accuracy ≥ Baseline (0.85) mit Fall-Report.
- pyproject: Marker registrieren, `-m "not eval"` in addopts (Gate bleibt
  offline), Eval-Verzeichnis von Coverage-Pflicht ausgenommen.
- CI: eigener Job, der den Key aus Secrets nimmt und sich selbst überspringt,
  wenn keiner gesetzt ist.
- AGENTS.md: `uv run pytest -m eval` dokumentiert.

## Out of scope

- Labeling-UI, Produktions-Daten-Mining, LLM-as-judge für Freitext (Bewerbungen)
  — erst wenn das Verdict-Eval trägt.

## Build steps

- [x] **Step 1 — Golden-Set + Runner** — Dataset + Test; ohne Key sauber
  geskippt. *Done when:* `uv run pytest -m eval` ohne Key skippt, Gates grün.
- [x] **Step 2 — CI + Doku** — Workflow-Job + AGENTS.md. *Done when:* YAML valide.

## Data / contracts

- Golden-Fall: `{id, description, expected_verdict, note}` — `expected_verdict`
  ∈ {match, no_match}. Binär, wie von den Pattern-Büchern empfohlen (keine
  Score-Regression, LLMs sind schlecht in Numerik).

## Testing

- Der Runner IST der Test. Ohne Key: skip, nicht fail.

## Notes for the AI

- Threshold-Logik nachbauen wie die Pipeline: `is_match_notifiable` +
  `enforce_nogo` — nicht das rohe Modell-Verdict vergleichen.
- Fälle realistisch deutsch, wie freelancermap-Listings.
