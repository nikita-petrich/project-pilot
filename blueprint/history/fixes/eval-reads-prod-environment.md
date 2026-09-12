# Current Feature

> **Fix spec.** Ad-hoc fix, built on the current branch.

## Fix: Eval-Workflow liest die Secrets aus dem prod-Environment

## The problem

GitHub hat zwei getrennte Secret-Ablagen, und ein Workflow sieht immer nur eine:

| Workflow | `environment:` | sieht |
|---|---|---|
| `deploy.yml` | `prod` (Zeile 75) | die `prod`-Ablage |
| `eval.yml` | — | nur die Repository-Ablage |

Dadurch müssen `LLM_PROVIDER`, `LLM_MODEL` und der API-Key **doppelt** gepflegt
werden. Fehlen sie auf Repository-Ebene, überspringt sich die Eval kommentarlos
(„LLM key/LLM_MODEL not configured — skipping", Exit 0) — der Lauf ist grün, ohne
dass ein einziger Golden-Fall geprüft wurde. Das ist genau die Sorte stiller
Ausfall, die das Gate verhindern soll: nach einem Provider-Wechsel verlässt man
sich auf eine Eval, die gar nicht lief.

## The fix

`environment: prod` in den `golden-set`-Job von `.github/workflows/eval.yml`.
Danach liest die Eval dieselben Secrets wie der Deploy — eine Ablage, ein Ort für
`LLM_PROVIDER`/`LLM_MODEL`/Key, und die Eval urteilt garantiert mit derselben
Konfiguration wie der Worker in Produktion.

Zwei bekannte Nebenwirkungen, beide akzeptiert:

- **Protection Rules.** Hätte `prod` Required reviewers oder einen Wait timer,
  würde die Eval darauf warten. Nik bestätigt, dass dort nichts gesetzt ist.
- **Deployment-Historie.** Eval-Läufe erscheinen künftig in der Deployment-Liste
  des `prod`-Environments. Kosmetisch, kein Verhalten.

Darf nicht brechen: die Skip-Logik bleibt, damit ein Lauf ohne Key weiterhin
sauber übersprungen statt rot wird (u.a. für PRs aus Forks, die keine
Environment-Secrets bekommen).

## Build steps

- [x] **1. `environment: prod` im golden-set-Job.** Eine Zeile in
      `.github/workflows/eval.yml`, plus ein Kommentar, der sagt *warum* (sonst
      trägt der nächste Leser sie wieder aus). Der Header-Kommentar der Datei
      erwähnt die Repository-Ablage nicht mehr.
      *Done when:* die Datei parst als gültiges YAML, der Job trägt
      `environment: prod`, und die Skip-Bedingung ist unverändert.

## Verify

1. YAML parst (`python -c "import yaml; yaml.safe_load(...)"`).
2. Quality-Gate grün: `ruff`, `ruff format --check`, `mypy`, `pytest`.
3. **Der echte Beweis, nach dem Merge:** *Actions → Eval → Run workflow*. Der Lauf
   darf sich nicht mehr mit „not configured — skipping" beenden, sondern muss die
   14 Golden-Fälle tatsächlich bewerten.


## Outcome

Gebaut und gemerged mit grünem Quality-Gate. YAML geprüft: der `golden-set`-Job
trägt `environment: prod`, die Skip-Bedingung ist unverändert.

Verify-Punkt 3 (ein echter Eval-Lauf, der die 14 Golden-Fälle wirklich bewertet)
steht noch aus — er braucht Credits und ist zugleich das offene Gate des
vorangegangenen Fixes `llm-provider-switchable`.
