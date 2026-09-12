# Fix: Chat-Link statt Code-Session pro Match

> Ad-hoc-Änderung, gebaut auf `claude/project-pilot-chat-core-links-q8scha`
> (2026-09-12).

## The problem

Der Bewerben-Button öffnete eine Claude-Code-Session auf diesem Repository.
Die stand vor der ersten Antwort bei ~97k Tokens Kontext: ~42k Claude-Code-
Systemprompt und -Tools, ~13k `CLAUDE.md` samt Imports, ~8k für 18 Repo-Skills,
dazu ein erster Turn, der die Karte wiederholte und das ganze Listing las.
Nichts davon dient einem Match; das Wochenlimit stand bei 78 %.

## Research (offizielle Quellen)

- Telegram-Buttons nehmen nur "HTTP or tg:// URL" (Bot API,
  `InlineKeyboardButton.url`). Die dokumentierten Chat- und Cowork-Deep-Links
  (`claude://claude.ai/new?q=`, `claude://cowork/new?q=`) sind Desktop-only und
  in Telegram nicht erlaubt.
- Eine Code-Session braucht immer ein Repository (Web-Quickstart).
- `https://claude.ai/new?q=…` füllt den Chat-Composer vor — getestet, aber in
  keiner Doku. `https://claude.ai/project/<id>?q=…` tut es nicht (getestet).
- Am Handy öffnet der Chat-Link die Claude-App (getestet; dokumentiert sind nur
  `claude.ai/code/…`-Links für die App).

## The fix

- `notification/claude_link.py`: Basis `https://claude.ai/new`, nur `q`, kein
  `repo`. Der Brief wiederholt die Karte nicht mehr und lädt das Listing nur
  "bei Bedarf".
- `CLAUDE_SESSION_REPO` → `CLAUDE_SESSION_URL` (Default Chat;
  `https://claude.ai/code/new` schaltet auf die dokumentierte Code-Session
  zurück, falls der Chat-Parameter verschwindet).
- Tests, `.env.example`, `docs/claude-setup.md`, README, AGENTS.md,
  Projektübersicht nachgezogen.

## Verified

`uv run ruff check`, `uv run ruff format --check`, `uv run mypy`,
`uv run pytest` (408 passed). Bewerben am Handy: Claude-App öffnet den Chat mit
der Karte im Composer.
