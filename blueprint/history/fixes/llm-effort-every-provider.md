# Fix: LLM_EFFORT gilt für jeden Anbieter, und der CI-Eval liest die echte Konfiguration

## The problem

Der Wechsel des Modells sollte ein reiner Secret-Wechsel im `prod`-Environment sein
(`LLM_PROVIDER`, `LLM_MODEL`, `LLM_EFFORT`, Key). Zwei Lücken standen dem im Weg:

- **`LLM_EFFORT` wurde bei `LLM_PROVIDER=openai` stillschweigend verworfen.** Nur die
  Anthropic-Adapter reichten es als `output_config.effort` weiter; die OpenAI-Adapter
  kannten den Wert nicht. Ein reasoning-fähiges OpenAI-Modell wie `gpt-5.6-luna`
  lief damit auf seinem eigenen Default (`medium`), egal was die ENV sagte, und
  verbrannte Output-Tokens für Reasoning, das ein Verdict nicht braucht.
- **`eval.yml` las `vars.LLM_PROVIDER` / `vars.LLM_MODEL`.** Im `prod`-Environment
  liegen beide als *Secrets*, Variables existieren nicht. Der Golden-Set-Job sah also
  leere Werte und skippte grün, ohne je einen Fall zu bewerten.

## The fix

- `openai_effort()` in `evaluation/llm.py`, das Gegenstück zu `anthropic_effort()`:
  liefert den Wert als `reasoning_effort` oder das `omit`-Sentinel des OpenAI-SDK, so
  dass ein leeres `LLM_EFFORT` weiterhin keinen Parameter sendet. Die fünf Stufen
  `low … max` sind auf beiden APIs gültig, also bleibt es bei einer Variablen.
- `OpenAiStructuredClient` und `OpenAiDraftClient` nehmen `effort` entgegen und
  senden es; beide Factories reichen `credentials.effort` durch.
- `eval.yml` liest die vier LLM-Werte aus `secrets` und bekommt `LLM_EFFORT` dazu,
  damit die Eval mit exakt der Prod-Konfiguration urteilt.
- Docs (`README.md`, `docs/deployment.md`, `.env.example`, `config.py`) sagen nicht
  mehr "Anthropic only".

## Done when

- [x] `openai_effort("")` ist das OpenAI-`omit`, `openai_effort("low") == "low"`
- [x] `structured_client` / `draft_client` geben bei `openai` das konfigurierte
      Effort an die Adapter
- [x] Quality Gate grün (ruff, format, mypy, pytest)
- [x] Wechsel auf ein anderes Modell ist ausschließlich ein Secret-Update im
      `prod`-Environment plus ein Deploy
