# project-pilot

A personal, single-user worker that watches freelancermap.de for new project
listings, persists every listing losslessly in PostgreSQL, evaluates fresh ones
against a profile (deterministic hard rules, then an LLM match), and reports real
matches to Slack within minutes. Backend only, no web UI.

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
4. Posts a Slack alert for matches at or above `MATCH_THRESHOLD`, and stores a
   reason for every verdict (match and no-match alike) for later reporting.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 (locally via `compose.dev.yaml`, or your own instance)
- A Slack app (Socket Mode, see below) and an OpenAI API key for live operation
- Docker with Compose for the containerized home-server deployment

## Setup

```sh
uv sync                              # install dependencies (creates .venv)
cp .env.example .env                 # then fill in the values (see below)
cp profile/profile.example.md profile/profile.md   # then fill in your real profile
docker compose -f compose.dev.yaml up -d   # local Postgres on :5432
uv run project-pilot init-db         # apply migrations
```

Fill in the two profile files (they feed the matcher, the hard rules, and the
application drafts):

- `profile/profile.md` free-text profile: positioning, skills, desired projects,
  no-gos, reference projects, and the application signature. **This file is
  gitignored and stays local** (it holds personal CV/contact data) — a sanitized
  `profile/profile.example.md` template is tracked instead, like `.env.example`.
  Its contact block ends with the two Notion Calendar booking links
  (`CTA German` / `CTA English`); the application generator picks the one matching
  the application language for the closing sentence and the LinkedIn message.
- `profile/constraints.yaml` hard rules (blacklist terms, optional must-have)

Set the environment values in `.env` (never commit real secrets; `.env` is
gitignored and `.env.example` is the template):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `CONTACT_MAIL` | inserted into the scraper user agent |
| `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` / `SLACK_CHANNEL` | Slack bot token (`xoxb-…`), app-level token for Socket Mode (`xapp-…`), and the channel id to post to |
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
uv run project-pilot daemon         # scheduler + Slack bot until SIGTERM
uv run project-pilot bot            # only the Slack bot (no scanning)
uv run project-pilot test-notify    # post a test message to the Slack channel
uv run project-pilot stats          # reporting summary
uv run project-pilot healthcheck    # liveness/freshness probe (exit code)
uv run project-pilot enrich "<company>" [--person "First Last"] [--url https://…]
uv run project-pilot enrich --listing-id <id>   # enrich a stored listing, record the lead
```

## Slack setup

The full, reproducible setup — creating (or re-creating) the app from a manifest,
tokens, channel, and scopes — lives in [`docs/slack-app-setup.md`](docs/slack-app-setup.md).
In short: create the app at [api.slack.com/apps](https://api.slack.com/apps) →
**From a manifest**, paste the manifest below, then generate the app-level token
(`connections:write` → `SLACK_APP_TOKEN`), install the app (`xoxb-…` →
`SLACK_BOT_TOKEN`), and set `SLACK_CHANNEL` to the channel id the bot is invited to.

```yaml
display_information:
  name: project-pilot
features:
  bot_user: { display_name: project-pilot, always_online: true }
  slash_commands:
    - { command: /apply, description: Create an application, usage_hint: "<link or text>", should_escape: false }
    - { command: /check, description: Check a listing against your profile, usage_hint: "<link or text>", should_escape: false }
oauth_config:
  scopes: { bot: [chat:write, commands, channels:history, files:read] }
settings:
  event_subscriptions: { bot_events: [message.channels] }
  interactivity: { is_enabled: true }
  socket_mode_enabled: true
```

Socket Mode means the app connects out to Slack — no public URL, works behind NAT.

## Applying from Slack

Every match posts a Slack message with an **📝 Bewerben** button. Tapping it makes
the LLM write a personalized application. The single prompt file
`src/project_pilot/application/prompts/application.md` holds the full application
prompt (style rules, reference projects, skills, signature) — edit it directly to
change how applications are written. The draft posts as **one** message:

- **Full e-mail** in copyable code blocks (split across Block Kit sections when
  long, never truncated), plus the subject and the LinkedIn message. Whenever a
  contact person is known, a **🔍 … on LinkedIn** button under the LinkedIn text
  opens a LinkedIn people search for that name (also on the post-send
  confirmation in the thread).
- **Recipient** — auto-extracted from the listing when an e-mail address is
  visible anywhere in it; otherwise reply in the thread with the address.
- **Revise** — reply in the message's thread with what you want changed
  ("kürzer", "auf Englisch", "betone RAG-Erfahrung") and the draft updates in place.
- **Buttons** — **📤 Senden** delivers the e-mail through your SMTP server
  (double-taps guarded, failures keep the draft); **❌ Verwerfen** cancels. The
  **📧 Open in mail client** link above the buttons opens your mail client with
  subject (and recipient, once known) prefilled — available from the start. It is
  a text link, not a button, because Slack buttons silently drop `mailto:` URLs.
- **CV attachment** — the sent e-mail attaches your CV automatically, picking the
  language that matches the draft (`CV_EN_PATH` for English, otherwise `CV_DE_PATH`);
  the letter references it. Leave the paths unset to send without an attachment.

`/apply <freelancermap-link>` starts the same flow for any listing (stored or
freshly fetched), and `/apply <pasted project description>` works without a link.
**Uploading a file** (PDF or text) to the channel drafts from its contents the same
way — drop in a project-description PDF and the draft appears. Nothing is ever sent
without the explicit Send tap.

## Finding a contact (enrichment)

Optional, **off by default** (`ENRICHMENT_ENABLED=true` to switch on). When a match
lists a company but no reachable e-mail, tap **🔎 Find contact** on the match (or on a
recipient-less draft) and project-pilot looks the company's contact channel up:

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
   request to the Ansprechpartner yourself. Set `APPLICANT_NAME` to sign it, and
   `OUTREACH_OFFER_DU=true` (the default) to offer first-name terms ("Gerne auch per Du.").

The result posts in the message's thread (e-mails best-first, phone, named people, the
connection message, the links) and is stored in `contact_leads`. Reply to a draft's
thread with a found address to set it as the recipient. From the shell:

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

## Checking a listing from Slack

`/check <freelancermap-link or pasted project description>` runs any listing through
the same evaluation the scanner uses — hard rules from `constraints.yaml` first
(0 tokens), then the LLM match against your profile:

- **Match (score ≥ `MATCH_THRESHOLD`)** — posts the full match message you know from
  the scanner (all listing facts, reasons, gaps, risks) including the **📝 Bewerben**
  button, so the apply flow starts exactly as if the scanner had found it.
- **No match** — posts the verdict with the failed hard rule (matched blacklist
  term) or the LLM's score, reasons, and gaps, so you see *why* it doesn't fit.
- **Files** — upload a PDF/text file with a comment containing `check` and the
  extracted text is checked instead of drafted (a comment without `check` keeps the
  usual upload-to-apply behavior).

A check is read-only: nothing is stored, the freshness gate is skipped, and the
scanner's watermark stays untouched.

## Running on the home server (Docker)

```sh
docker compose build
docker compose up -d
docker compose logs -f app
```

The app container applies migrations on start, then runs the daemon (with one
immediate scan). Full operations guide, including the healthcheck and
troubleshooting, is in [`docs/operations.md`](docs/operations.md).

## Threshold tuning

After a few days, run `stats` and inspect the verdict distribution and stored
scores. Raise `MATCH_THRESHOLD` if you get too many weak alerts; lower it if real
matches are missed. Restart the worker after changing `.env`.

## Troubleshooting

- **Cooldown**: a 403 or captcha sets a 6-hour cooldown in `source_state`; the
  worker skips scans until it expires and posts one Slack warning.
- **`SelectorMismatchError`**: freelancermap changed its markup. Update the
  selector constants at the top of `src/project_pilot/ingestion/parser.py`,
  refresh the fixtures, and re-run.
- **Repeated failures**: three consecutive failed runs post one Slack warning.
- **Container unhealthy**: no successful run within three times the interval;
  check `docker compose logs app`.

## Development

```sh
uv run ruff check           # lint
uv run ruff format --check  # format
uv run mypy                 # strict type check
uv run pytest               # tests (Postgres-backed tests skip if no DB)
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
  notification/ slack
  application/  generator, service, mailer, documents (apply flow)
  pipeline.py scheduler.py reporting.py cli.py
alembic/        async migrations
docs/           compliance.md, operations.md, adr/
tests/          unit + integration, fixtures/
```

Built with the [AI Coding Blueprint](blueprint/README.md); agent instructions live
in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
