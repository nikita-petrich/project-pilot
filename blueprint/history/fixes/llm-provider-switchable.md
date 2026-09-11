# Current Feature

> **Fix spec.** Ad-hoc fix, built on the current branch via Autopilot.

## Fix: LLM-Provider austauschbar machen (OpenAI oder Anthropic)

## The problem

Die OpenAI-Credits sind abgelaufen. Damit fällt Stage 3 in den `llm_error`-Fallback:
der Scanner sammelt und speichert weiter, aber jedes Listing bekommt
`verdict=no_match`, und **es kann kein Match-Alert mehr feuern**. Derselbe Ausfall
träfe auch den Bewerbungs-Draft (`ApplicationGenerator`) und den `check`-Pfad im
MCP-Server.

Der Anbieter ist an sieben Stellen fest verdrahtet, obwohl die Architektur den
Wechsel schon vorbereitet:

| Stelle | Kopplung |
|---|---|
| `evaluation/llm.py` | `OpenAiStructuredClient` — hinter dem Protocol `StructuredLlmClient` |
| `application/generator.py` | `OpenAiDraftClient` — hinter `StructuredDraftClient` |
| `cli.py` | drei Konstruktionsstellen (`_build_pipeline`, `_build_mcp_app`, `_run_selftest`) |
| `config.py` | `openai_api_key`, `require_openai()` |
| `health.py` | `from openai import APIConnectionError`; alle Meldungstexte sagen „OpenAI" |
| `tests/eval/test_golden.py` | baut `OpenAiStructuredClient` direkt |
| `.env.example`, `deploy/render-env.py`, `.github/workflows/eval.yml`, Docs | `OPENAI_API_KEY` als harte Pflicht |

## The fix

Ein **zweiter Adapter pro Protocol**, ausgewählt über `LLM_PROVIDER`. Beide Anbieter
bleiben im Code — das ist der Rückweg, falls die Golden-Set-Eval zeigt, dass
`match.v7` auf dem neuen Modell anders kalibriert.

Bewusste Entscheidungen:

- **Kein `thinking`, kein `output_config.effort` im Adapter.** `LLM_MODEL` kommt frei
  aus der ENV (Coding-Standard: nie hardcoden); ein fest gesetzter Thinking-Parameter
  würde bei manchen Modellen mit 400 abbrechen. Die Modell-Defaults gelten.
- **`MatchVerdict` und `ApplicationDraft` bleiben unverändert.** Anthropic nimmt
  dieselben Pydantic-Modelle über `messages.parse(output_format=...)`.
- **`ANTHROPIC_API_KEY` kehrt zurück**, aber in anderer Rolle als vor Feature 27: dort
  war es der Schlüssel des gelöschten Thread-Agents, hier ist es der LLM-Client.

Darf nicht brechen: der bestehende OpenAI-Pfad (Default-Provider bleibt `openai`),
die `llm_error`-Fallback-Garantie, und die Fehlerklassifikation in `health.py` — die
muss beim neuen Anbieter **mindestens so gut** diagnostizieren wie beim alten.

## Build steps

- [x] **1. Config: Provider-Auswahl.** `llm_provider` (`openai` | `anthropic`, Default
      `openai`) + `anthropic_api_key` in `Settings`; Validator wie `_known_log_level`.
      `require_openai()` wird zu `require_llm() -> LlmCredentials(provider, api_key, model)`
      und nennt in der `ConfigError` die ENV-Variable des *gewählten* Anbieters.
      *Done when:* `test_config.py` zeigt für beide Provider den richtigen Schlüssel und
      für einen unbekannten Provider einen Boot-Fehler.

- [x] **2. Health: anbieterneutrale Klassifikation.** Anthropic-Fehler haben **kein**
      `.code` und melden ein leeres Guthaben als **HTTP 400 `invalid_request_error`**
      mit „credit balance is too low" — mit dem heutigen `_llm_kind` wäre das `UNKNOWN`
      und damit fälschlich *retryable*. Also: `.type` und den Message-Text mit auswerten,
      `APIConnectionError` beider SDKs erkennen, und die „OpenAI"-Texte in
      `_LLM_SUMMARIES` durch den Provider-Namen ersetzen.
      *Done when:* `test_health.py` deckt Anthropic-400-Guthaben → `QUOTA` (nicht
      retryable), 401 → `AUTH`, 404 → `MODEL_NOT_FOUND` ab, und die OpenAI-Fälle bleiben grün.

- [x] **3. Anthropic-Adapter.** `AnthropicStructuredClient` in `evaluation/llm.py` und
      `AnthropicDraftClient` in `application/generator.py`, beide über
      `messages.parse(output_format=...)` mit `system=` als eigenem Parameter. Bilder
      brauchen die Anthropic-Form (`{"type": "image", "source": {"type": "base64", ...}}`)
      statt der OpenAI-`image_url`-Data-URL — dafür ein eigener Content-Builder neben
      `build_user_content`.
      *Done when:* Unit-Test für den Content-Builder (Text ohne Bilder bleibt ein String;
      mit Bildern kommen Image-Blöcke vor dem Text), `ruff`/`mypy` grün.

- [x] **4. Verdrahtung.** Je eine Factory (`structured_client(creds)`,
      `draft_client(creds)`) neben den Adaptern; die drei `cli.py`-Stellen und
      `tests/eval/test_golden.py` nutzen sie statt der OpenAI-Klasse.
      *Done when:* `uv run project-pilot --help` läuft, `test_cli.py` grün, kein
      `OpenAi*Client` mehr außerhalb von `evaluation/llm.py` / `application/generator.py`.

- [x] **5. Konfigurationsoberfläche.** `.env.example` (Provider-Block mit beiden
      Schlüsseln), `deploy/render-env.py` (Pflichtfeld wird der Schlüssel des gewählten
      Providers, nicht mehr fix `OPENAI_API_KEY`), `.github/workflows/eval.yml`
      (beide Secrets, Skip-Bedingung anpassen), `README.md`, `docs/deployment.md`,
      `docs/operations.md`, `AGENTS.md`, `HANDOVER.md`.
      *Done when:* `render-env.py` akzeptiert beide Anbieter und verweigert einen ohne
      Schlüssel; die Docs nennen nirgends mehr OpenAI als einzige Option.

## Verify

1. Quality-Gate komplett grün: `uv run ruff check`, `uv run ruff format --check`,
   `uv run mypy`, `uv run pytest`.
2. `uv run project-pilot --help` und ein Settings-Boot mit `LLM_PROVIDER=anthropic`
   ohne Schlüssel → klare `ConfigError`, die `ANTHROPIC_API_KEY` nennt.
3. **Das eigentliche Gate, manuell und außerhalb dieses Laufs:** mit echtem Schlüssel
   `LLM_PROVIDER=anthropic LLM_MODEL=… uv run pytest -m eval` gegen
   `tests/eval/golden.jsonl` (14 Fälle). Erst dieses Ergebnis entscheidet, ob
   `MATCH_THRESHOLD` oder `match.v7` nachgezogen werden müssen. Ohne Credits hier im
   Container nicht ausführbar — bleibt als offener Punkt im Review-Paket stehen.


## Outcome

Gebaut und gemerged mit grünem Quality-Gate (`ruff`, `ruff format`, `mypy --strict`,
403 Tests). Belegt wurden: beide Provider an allen Einstiegspunkten (`daemon`/`run-once`,
MCP-Server, Draft-Generator), die klare `ConfigError` ohne den Schlüssel des gewählten
Anbieters, und die Klassifikation des leeren Anthropic-Guthabens als `quota` (nicht
retryable).

**Offen geblieben, bewusst:** die Golden-Set-Eval (Verify-Punkt 3) lief nicht — es
fehlten genau die Credits, die diesen Fix ausgelöst haben. `match.v7` ist gegen ein
OpenAI-Modell getuned, also ist noch nicht bewiesen, dass Anthropic gleich urteilt.
Vor dem Umschalten in Produktion:

    LLM_PROVIDER=anthropic LLM_MODEL=<modell> uv run pytest -m eval

Danach ggf. `MATCH_THRESHOLD` oder den Prompt nachziehen.

Zwei Selbstreview-Funde wurden im Lauf behoben: die Factories fielen für einen
unbekannten Provider still auf OpenAI zurück (jetzt `ConfigError`), und der
Verdict-Token-Cap war zu knapp für ein Modell, das den Cap mit Thinking-Tokens teilt.

Nicht angefasst: `SPEC.md` und `blueprint/context/project-overview.md` nennen weiter
OpenAI als einzigen Anbieter — SPEC.md ist historisch, die Overview regeneriert
`/overview`.
