# project-pilot

A personal, single-user worker that watches freelancermap.de for new project
listings, persists every listing losslessly in PostgreSQL, evaluates fresh ones
against a profile (deterministic hard rules, then an LLM match), and pushes real
matches within minutes: each match opens its own Claude session, and the Claude
app delivers it to phone and laptop. Backend only, no web UI — the Claude app is
the entire interaction surface.

Built as a modern, strictly-typed Python codebase (Python 3.13, asyncio,
Pydantic v2, SQLAlchemy 2.0, `mypy --strict`). The binding detail specification is
[`SPEC.md`](SPEC.md); the design is summarized in
[`blueprint/context/project-overview.md`](blueprint/context/project-overview.md).

## How it works

Every `SCAN_INTERVAL_MIN` minutes (default 15) the worker:

1. Fetches the configured search URLs politely (identifying user agent, robots.txt
   gate, delays), paginating with a watermark so nothing is missed after downtime.
2. Persists every newly seen listing (lossless). On an empty database the first
   run seeds the full inventory without analysing or notifying.
3. For each new, fresh listing (within the analysis window), runs the evaluation
   pipeline: freshness gate, then hard rules from `constraints.yaml` (0 tokens),
   then an LLM match against `profile.md` producing a structured verdict.
4. Sends a Telegram notification for every match at or above
   `MATCH_THRESHOLD`. That opens one Claude session carrying the match card, the
   remaining facts and the full description; Claude repeats the card and adds its
   own reading, and the Claude app pushes the finished session to your phone and
   laptop. A reason is stored for every verdict — match and no-match alike — for
   later reporting.

The card is rendered in code (`notification/messages.py`), not left to the model,
so every alert is scannable the same way:

```
🎯 Senior Backend Entwickler (Node.js)  ·  87/100
🏢 One Day Ahead GmbH  ·  Agency
📍 Frankfurt am Main, Deutschland  ·  🏠 100%
📅 01.09.2026  ·  ⏳ 4 mo (+ extension)  ·  📊 100%  ·  🕒 5 min ago
✅ Fits: Node.js-/Express.js-Stack deckt den Backend-Fokus, REST, Docker und PostgreSQL sind im Profil abgedeckt
⚠️ Risks: Agentur-Listing, Endkunde nicht genannt
🔗 https://www.freelancermap.de/projekt/…
```

Company and location always get their line: a listing that names neither is
itself a signal, so the card says so rather than dropping it silently.

Everything after the push happens in that session: ask questions, have the
application drafted and revised, and send it, through the MCP server this project
also ships. Operator warnings (source cooldown, LLM health, repeated failures)
arrive the same way, as their own sessions.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 (locally via `compose.dev.yaml`, or your own instance)
- An OpenAI API key, and a Claude plan with Claude Code on the web for the
  Telegram notification and the Claude surface ([`docs/claude-setup.md`](docs/claude-setup.md))
- Docker with Compose for the containerized home-server deployment

## Setup

```sh
uv sync                              # install dependencies (creates .venv)
cp .env.example .env                 # then fill in the values (see below)
docker compose -f compose.dev.yaml up -d   # local Postgres on :5432
uv run project-pilot init-db         # apply migrations
```

Forking this for yourself? Replace `profile/profile.md` (start from
`profile/profile.example.md`) and the PDFs in `cv/` with your own.

The two profile files feed the matcher, the hard rules, and the application drafts:

- `profile/profile.md` free-text profile: positioning, skills, desired projects,
  no-gos, reference projects, and the application signature. It is **versioned
  on purpose** — this repo is a public portfolio piece, and it holds the same CV
  and contact block that goes out to clients anyway. Real secrets stay in `.env`.
  Its `Contact & Signature` block holds the values for the e-mail signature (name,
  title, `Phone`, `Email`, `Web`, `LinkedIn`, `GitHub`, plus `Location German` /
  `Location English` and `VAT ID`); the layout itself lives in the prompt, which
  looks these keys up by name — rename one there and here together. It ends with the two
  Notion Calendar booking links (`CTA German` / `CTA English`); the application
  generator picks the one matching the application language for the closing
  sentence, the signature, and the LinkedIn message.
- `profile/constraints.yaml` deterministic rules: `blacklist` terms and an optional
  `must_have`, both matched against the listing text before the LLM (0 tokens), plus
  `nogo_technologies`. The last one is the profile's context-dependent no-gos (Java,
  PHP, WordPress, Django, SAP): they are deliberately **not** matched against the
  listing text — a frontend role against a Java backend or a migration away from PHP
  stays welcome — but against the LLM's own answer. When the model reports one of
  them under `missing_requirements`, i.e. the listing requires the candidate to bring
  it and the profile does not cover it, the verdict is forced to `no_match` whatever
  the score, and the stored reason names the term (`nogo`). Matching is
  case-insensitive with word boundaries, so `java` never fires on "JavaScript".
- `cv/` the CVs attached to application e-mails — **both ride along on every
  send**, so the recipient can forward whichever language they need.
  `CV_DE_PATH` and `CV_EN_PATH` default to `cv/CV-German.pdf` and
  `cv/CV-English.pdf` — the two PDFs versioned here — so
  updating a CV is replacing the file and pushing. The file name is what the
  recipient sees. A configured file that is not on disk is skipped and named in the
  draft's `📎 Attachments` line, so you can add them one at a time. Keep them a few
  MB at most — base64 adds about a third on the wire.

Set the environment values in `.env` (never commit real secrets; `.env` is
gitignored and `.env.example` is the template):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `CONTACT_MAIL` | inserted into the scraper user agent |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | the bot from @BotFather and the chat it sends to — the notification channel |
| `MCP_TOKEN` / `MCP_PORT` | bearer token for the MCP server (`openssl rand -hex 32`) and its port (default 8765) |
| `ANTHROPIC_API_KEY` | the thread agent's own key (`MCP_URL` defaults to the mcp service next to it) |
| `TELEGRAM_ALLOWED_USER_IDS` | who may drive that agent — anyone else's message is dropped |
| `AGENT_MODEL` / `AGENT_WORKSPACE` | model (default `claude-opus-5`) and the directory the agent works in |
| `PROXY_NETWORK` | VPS only: the Docker network the reverse proxy runs on, so it can reach `project-pilot-mcp` |
| `OPENAI_API_KEY` / `LLM_MODEL` | LLM matching (a small model is enough) |
| `SEARCH_URLS` | comma-separated board search URLs, sorted "newest first" |
| `SCAN_INTERVAL_MIN` | default 15, validated to be >= 15 |
| `ANALYSIS_WINDOW_MIN` | default 30 |
| `MATCH_THRESHOLD` | default 60 |
| `LOG_LEVEL` | default `info` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` | your mail server, used to send application e-mails (port 465 implies TLS, otherwise STARTTLS) |
| `SMTP_FROM` / `SMTP_STARTTLS` | optional sender override (defaults to `SMTP_USER`) and STARTTLS toggle |

## Commands

```sh
uv run project-pilot init-db        # apply Alembic migrations
uv run project-pilot run-once       # one scan now (non-zero exit on a failed run)
uv run project-pilot daemon         # the scan loop until SIGTERM
uv run project-pilot mcp            # the MCP server (Streamable HTTP + bearer token)
uv run project-pilot test-match     # rules + LLM + a real push, stores nothing
uv run project-pilot test-filter    # dry-run the filter against a listing
uv run project-pilot stats          # reporting summary
uv run project-pilot healthcheck    # liveness/freshness probe (exit code)
uv run project-pilot enrich "<company>" [--person "First Last"] [--url https://…]
uv run project-pilot enrich --listing-id <id>   # enrich a stored listing, record the lead
```

## Notification and Claude setup

Three pieces, all in [`docs/claude-setup.md`](docs/claude-setup.md):

1. **The Telegram channel post**, sent by the worker itself: one post per match
   carrying the card and its three decisions. Telegram forwards each post into
   the channel's linked discussion group and roots a comment thread on it, so a
   project is one post you open into its own conversation. Send-only from the
   worker — no polling, no webhook, no inbound port — and delivery never depends
   on a model judging a run worth reporting.
2. **The thread agent** (`project-pilot telegram-bot`), a full Claude Code agent
   on the Claude Agent SDK that answers inside those threads. Reading runs
   freely; writing, running a command and sending ask for a button press first.
   Its domain layer is the MCP server next door, so profile, judging rules and
   writing style have one home. The proxy's site config, for the public
   endpoint Claude chats and n8n use, is in
   [`deploy/proxy-site/`](deploy/proxy-site).
3. **The workflow prompts**, exposed by the MCP server itself
   (`mcp_prompts.py`), so one definition serves every surface: Claude Code
   lists them as `/mcp__project-pilot__check_project`, a bot renders its own
   command menu from `prompts/list`, and n8n calls them the same way. The
   account skills in [`deploy/claudeai-skills/`](deploy/claudeai-skills) are
   thin wrappers over the same tools for surfaces that don't show MCP prompts.

Ten tools are exposed, and any Claude chat that has the connector can use them:

| Tool | Does |
|---|---|
| `list_matches` / `get_listing` | the feed and one listing in full, with its evaluations |
| `ingest_listing` | store a listing that did not come from the scanner, with its provenance |
| `check_listing` / `check_text` | re-run the verdict on a stored listing or on pasted text |
| `draft_application` / `revise_application` | write and rework a draft |
| `set_recipient` / `send_application` | address it, and — only on your explicit go — send it |
| `enrich_company` | contact data from the company's own website |

n8n speaks the same protocol, so a workflow can ingest and check incoming
recruiter mails without duplicating any of the judgment.

### Listings from anywhere else

A recruiter mails, a client sends a PDF, someone screenshots a listing, an n8n
workflow forwards one from another board. `ingest_listing` stores it like any
other listing and records two things about where it came from:

| Column | Answers | Values |
|---|---|---|
| `listings.source` | **which platform** | `freelancermap`, `linkedin`, `malt`, an agency name, … — read off the URL, or passed in; `manual` for text with no URL |
| `listings.origin` | **which channel** | `scan`, `chat`, `mail`, `pdf`, `image`, `url`, `api` |

`raw["ingest"]` keeps the detail (a note, the supplied URL, the timestamp).
Everything downstream then works on it unchanged: check, draft, revise, send,
reporting.

Dedupe is the scanner's own: an absolute listing URL is canonicalized and hashed
exactly as the scraper does it, so a link you check by hand and the page the
scanner later fetches are **one** row, not two. Text with no URL is keyed by the
text, so the same mail pasted twice is the same listing. A bare path is never
resolved against the scraped board — it could belong to any host, so it is keyed
by its text instead of being guessed at.

### Which parts are tied to freelancermap

Only the scraper. Everything else was built source-agnostic and stays that way:

| Layer | Bound to a board? |
|---|---|
| `ingestion/parser.py`, `SEARCH_URLS`, `source_state` watermark | **yes** — freelancermap's HTML and pagination |
| data model (`listings.source` per row, `source_state` keyed by source) | no |
| evaluation (`constraints.yaml`, `match.v7.md`, the no-go gate) | no — neither prompt names a board |
| application drafting, enrichment, sending | no |
| MCP tools, the Telegram notification, the skills | no |

So a second board reaches the database today through `ingest_listing` (an n8n
workflow forwarding its mails costs no code at all), and *scanning* one is a new
parser plus its search URLs — build-plan item 15, deliberately not built yet.

## Applying from a match thread

Ask for it in the session — "schreib die Bewerbung" — and the LLM writes a
personalized application. The single prompt file
`src/project_pilot/application/prompts/application.md` holds the full application
prompt (style rules, reference projects, skills, signature); edit it directly to
change how applications are written.

- **Full draft** — subject, the complete e-mail, and a LinkedIn connection
  message, returned in one piece and never truncated.
- **Recipient** — auto-extracted from the listing when an e-mail address is visible
  anywhere in it; otherwise name it in the chat, or ask for the contact to be
  looked up (see below).
- **Revise** — say what you want changed ("kürzer", "auf Englisch", "betone
  RAG-Erfahrung") and the draft is rewritten in place. Paste or attach a screenshot
  and it goes to the model as vision input, so a picture of the client's reply or
  of a listing detail can drive the revision.
- **Send** — only after you have read the draft and said so. `send_application`
  delivers it through your SMTP server with the CVs attached; a status guard makes a
  second send impossible, and a failure keeps the draft intact. Nothing else in the
  system can reach outward, which is the point: the model reads untrusted listing
  text, so it never holds the outbound channel on its own.
- **CV attachments** — every sent e-mail carries both configured CV PDFs (DE and
  EN); the draft language only decides which one leads. The draft names them in a
  `📎 Attachments` line beforehand, including any configured file that is missing,
  so a gap is visible before the send rather than after.
- **Signature** — every draft closes with a signature block in the draft's language:
  the `-- ` separator (RFC 3676), the greeting inside the block, name and title,
  `Tel./Phone`, `E-Mail`, `Web`, `LinkedIn`, `GitHub`, the 30-minute booking link,
  then location and VAT ID — values from `profile.md`, layout from the prompt. The
  confidentiality notice follows as the last block.

The same flow works from any Claude chat with the connector, not just from a match
session: paste a listing, run `check_text`, then draft from it.

## Finding a contact (enrichment)

Enrichment is optional and **off by default** (`ENRICHMENT_ENABLED=true` to switch
on). When a match names a company but no reachable e-mail, ask for the contact in
the session (`enrich_company`) and project-pilot looks the company's contact
channel up:

1. **Company website** — a web search (`ENRICHMENT_SEARCH=duckduckgo`, or pass a known
   `--url`) finds the official site, then its **Impressum / Kontakt / Team / Karriere**
   pages are read for e-mails, phone numbers, and contact-person names. In Germany the
   Impressum is legally-required public contact data, so this is the reliable source of
   a phone/e-mail. Fetches are polite and robots-aware; a 403 is never retried.
2. **LinkedIn & Google** — surfaced as **one-click research links** (company search,
   people search, Google contact search). These open in your own browser; **nothing on
   LinkedIn or Google is ever scraped** — that would breach their terms, and LinkedIn
   never exposes phone/e-mail publicly anyway.
3. **LinkedIn connection message** — every result includes a short, personalized German
   **Vernetzungsnachricht** (≤300 chars, ready to copy) so you can send the connection
   request to the Ansprechpartner yourself. It signs with your name from
   `profile.md` (Contact & Signature); `OUTREACH_OFFER_DU=true` (the default) offers
   first-name terms ("Gerne auch per Du.").

The result comes back in the session (e-mails best-first, phone, named people, the
connection message, the research links) and is stored in `contact_leads`. Name a
found address in the chat to set it as the draft's recipient. From the shell:

```sh
uv run project-pilot enrich "Muster GmbH" --person "Max Mustermann"
uv run project-pilot enrich --listing-id 42     # uses the listing's company + records a lead
```

**JS-rendered sites (optional).** Some sites inject their contact data via JavaScript,
which the default httpx fetcher can't see. Set `ENRICHMENT_RENDER=true` to fetch company
pages with a headless Chromium instead — install the extra once:

```sh
uv sync --extra render && uv run playwright install chromium
```

Rendering keeps the same manners (identifying user agent, robots gate, delay, no 403
retry); only company pages are rendered, never LinkedIn or Google.

## Checking a listing on demand

`check_listing` (a stored listing) and `check_text` (a pasted description or
recruiter mail) run anything through the same evaluation the scanner uses — hard
rules from `constraints.yaml` first (0 tokens), then the LLM match against your
profile. Both return the verdict in full:

- **Match (score ≥ `MATCH_THRESHOLD`)** — facts, reasons, matching skills, gaps and
  risks, so the apply flow can start from it exactly as if the scanner had found it.
- **No match** — the failed hard rule (the matched blacklist term) or the LLM's
  score, reasons, and gaps, so you see *why* it doesn't fit. A listing that requires
  one of the `nogo_technologies` lands here too, with the term named first.

A check is read-only: nothing is stored, the freshness gate is skipped, and the
scanner's watermark stays untouched.

Two ways reach the same judgment without the server:
[`/check-project`](.claude/skills/check-project/SKILL.md) and
[`/write-application`](.claude/skills/write-application/SKILL.md) are skills any
Claude session with this repository can run. They are thin wrappers that read the
canonical prompt and profile files at runtime rather than restating their rules, so
a skill and the pipeline cannot drift apart — change a judgment rule in
`evaluation/prompts/match.v7.md`, never in the skill.

## Deploying

Pushing to `main` deploys to the VPS: GitHub Actions runs the quality gate, builds
the image into GHCR, and the server pulls it over SSH. The server holds no
configuration of its own — the app's `.env` is rendered from the secrets of the
`prod` environment and written on every deploy. Setup, secrets, and rollback are in
[`docs/deployment.md`](docs/deployment.md).

The deploy refuses to start if `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
or `MCP_TOKEN` is missing, and fails before touching the server: a worker that
finds matches it cannot deliver is worse than one that does not run.

To build and run on the host instead, from a checkout:

```sh
docker compose build
docker compose up -d
docker compose logs -f app
```

Either way the app container applies migrations on start, then runs the daemon (with
one immediate scan). Full operations guide, including the healthcheck and
troubleshooting, is in [`docs/operations.md`](docs/operations.md).

## Threshold tuning

After a few days, run `stats` and inspect the verdict distribution and stored
scores. Raise `MATCH_THRESHOLD` if you get too many weak alerts; lower it if real
matches are missed. Restart the worker after changing `.env`.

## Troubleshooting

- **Cooldown**: a 403 or captcha sets a 6-hour cooldown in `source_state`; the
  worker skips scans until it expires and opens one warning session.
- **`SelectorMismatchError`**: freelancermap changed its markup. Update the
  selector constants at the top of `src/project_pilot/ingestion/parser.py`,
  refresh the fixtures, and re-run.
- **Repeated failures**: three consecutive failed runs open one warning session.
- **Container unhealthy**: no successful run within three times the interval;
  check `docker compose logs app`.
- **Everything looks healthy but no matches arrive**: the LLM is the one dependency
  whose failure still produces successful runs (every listing falls back to
  `llm_error`). The daemon preflights `LLM_MODEL` on start and opens a warning
  session naming the cause — wrong model, rejected key, or an account out of credit
  — then announces recovery once it works again. See `docs/operations.md`.
- **No push for a match**: delivery failed. `docker compose logs app` shows
  `telegram send failed`; the listing keeps `notified_at` empty and the next scan
  retries it, so nothing is lost while the channel is down.

## Development

```sh
uv run ruff check           # lint
uv run ruff format --check  # format
uv run mypy                 # strict type check
uv run pytest               # tests (Postgres-backed tests skip if no DB)
uv run pytest -m eval       # judgment eval against the golden set (real LLM calls)
```

Tests never make live network requests; freelancermap pages and external APIs are
served from fixtures or mocked. Test files live next to the code under `tests/`.

## Compliance and legal

project-pilot is for **personal use only**. It reads only pages that are publicly
reachable without login, with a clear, identifying user agent
(`project-pilot/1.0 (personal project alert bot; contact: <CONTACT_MAIL>)`), a
startup `robots.txt` gate (including `Crawl-delay`), a 2 to 5 second delay between
requests, and at most two list pages per search URL (detail pages only for new
listings). There is no login bypass, no captcha or bot-protection circumvention,
and no user-agent or proxy rotation. A 403 or captcha triggers a 6-hour cooldown
rather than retry hammering.

Before the first live run, confirm freelancermap's current `robots.txt` and Terms
of Service permit this use. The full compliance record, the STOP conditions, and
the first-run verification checklist are in
[`docs/compliance.md`](docs/compliance.md) and
[`docs/adr/0001-source-verification.md`](docs/adr/0001-source-verification.md). Do
not resell or redistribute scraped data.

The same posture governs **contact enrichment**: it reads only a company's own public
website (the legally-required Impressum and its contact pages), with the identifying
user agent, a per-host `robots.txt` gate, a spacing delay, and no 403 retry. It **does
not scrape LinkedIn or Google** — those are only ever offered as search links you open
yourself. Enrichment is off unless you set `ENRICHMENT_ENABLED=true`, and it processes
personal contact data (names, e-mails, phone numbers) solely so you can apply to the
project — use it accordingly and do not store or share the results beyond that purpose.

## Project layout

```
src/project_pilot/
  config.py profile_loader.py errors.py db.py models.py repository.py
  ingestion/    client, parser, normalize, watermark
  evaluation/   freshness, rules, llm, schemas, check, prompts/
  enrichment/   fetch, render, search, extract, links, message, service, listing
  notification/ telegram (the channel), messages
  mcp_prompts   the workflow prompts, one source for every surface
  application/  generator, service, mailer, documents, cv_drive (apply flow)
  mcp_server.py the tools any Claude surface calls
  pipeline.py scheduler.py reporting.py cli.py
alembic/        async migrations
deploy/         render-env.py, remote-deploy.sh, proxy/ (Caddy for the MCP host)
docs/           claude-setup.md, deployment.md, operations.md, compliance.md, adr/
tests/          unit + integration, fixtures/, eval/ (golden set)
```

Built with the [AI Coding Blueprint](blueprint/README.md); agent instructions live
in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
