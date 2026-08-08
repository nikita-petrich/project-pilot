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
4. Posts a Slack alert for matches at or above `MATCH_THRESHOLD` — a compact card
   in the channel (title, score, client, location/remote, start, duration,
   workload, age, top reasons, buttons) with the full listing as its first thread
   reply — and stores a reason for every verdict (match and no-match alike) for
   later reporting.

## Requirements

- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL 16 (locally via `compose.dev.yaml`, or your own instance)
- A Slack app (Socket Mode, see below) and an OpenAI API key for live operation
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
- `profile/constraints.yaml` hard rules (blacklist terms, optional must-have)
- `cv/` the CVs attached to application e-mails — **all of them ride along on every
  send**, so the recipient can forward whichever format and language they need.
  `CV_DE_PATH` and `CV_EN_PATH` default to `cv/CV-German.pdf` and
  `cv/CV-English.pdf` — the two PDFs versioned here — so
  updating a CV is replacing the file and pushing. The Word slots
  (`CV_DE_DOCX_PATH`, `CV_EN_DOCX_PATH`) are unset by default; point them at a
  `.docx` only if you add one. The file name is what the
  recipient sees. A configured file that is not on disk is skipped and named in the
  draft's `📎 Attachments` line, so you can add them one at a time. Keep them a few
  MB at most — base64 adds about a third on the wire.

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
  opens a LinkedIn people search for `<name> AND <company>` (the company narrows
  it to the right person; also on the post-send confirmation in the thread).
- **Recipient** — auto-extracted from the listing when an e-mail address is
  visible anywhere in it; otherwise reply in the thread with the address.
- **Revise** — reply in the message's thread with what you want changed
  ("kürzer", "auf Englisch", "betone RAG-Erfahrung") and the draft updates in place.
  Attach a screenshot to the reply (with or without text) and it goes to the LLM
  as vision input — e.g. a picture of the client's answer or of the listing detail
  the revision should reflect.
- **E-mail as a file** — the letter itself arrives in the thread as one `.txt`
  file (nothing split across blocks): open, copy, or download it in one piece.
  Each revision uploads a fresh file; the newest one is the current draft. This
  needs the `files:write` bot scope and a channel *ID* in `SLACK_CHANNEL` — without
  them the bot falls back to rendering the text inline. All generated texts
  (LinkedIn message, contact results, inline fallbacks) render as native code
  blocks, so each has Slack's **copy button in its top-right corner**.
- **Buttons** — **📤 Send** delivers the e-mail through your SMTP server, CVs
  attached (double-taps guarded, failures keep the draft); **❌ Discard** cancels.
  Send is always visible: without a recipient it answers with the hint to reply with
  the address instead of sending. The **📧 Open in mail client** link above the
  buttons opens your mail client with the subject, the recipient (once known) and
  the letter prefilled — available from the start. It is a text link, not a button,
  because Slack buttons silently drop `mailto:` URLs. A `mailto:` has a length limit
  and can never carry attachments, so a long letter opens truncated (with a note
  saying so) and the CVs only go out via **📤 Send**.
- **CV attachments** — every sent e-mail carries all configured CVs (PDF and Word,
  DE and EN); the draft language only decides which one leads. The draft names them
  in a `📎 Attachments` line before you send, including any configured file that is
  missing on disk. Set a path to an empty value to leave that CV out.
- **Signature** — every draft closes with a signature block in the draft's language:
  the `-- ` separator (RFC 3676), the greeting inside the block, name and title,
  `Tel./Phone`, `E-Mail`, `Web`, `LinkedIn`, `GitHub`, the 30-minute booking link,
  then location and VAT ID — values from `profile.md`, layout from the prompt. The
  confidentiality notice follows as the last block.

`/apply <freelancermap-link>` starts the same flow for any listing (stored or
freshly fetched), and `/apply <pasted project description>` works without a link.

**Uploading a file** (PDF, text, or **image**) does the same. Slack cannot attach a
file to a slash command, so the bot asks instead: drop the file in the channel and
it replies **in the upload's thread** with two buttons — **📝 Apply** drafts the
application, **🔍 Check** scores the listing first. Nothing is downloaded and no
token is spent until you press one, and the buttons disappear once used, so an
upload can never fire twice.

Any comment you add to the upload is kept as extra project context (there is no
keyword to remember), and screenshots (PNG/JPEG/WebP/GIF) go to the vision LLM
directly. Nothing is ever sent without the explicit Send tap.

**Everything answers in a thread.** A slash command posts a single channel line
(`📥 Application: …`) and puts the draft, progress, and any hint in its thread; an
upload is answered in the **upload's own thread**, with no extra channel message. So
the channel keeps one line per request and the back-and-forth stays out of the way.

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
   request to the Ansprechpartner yourself. It signs with your name from
   `profile.md` (Contact & Signature); `OUTREACH_OFFER_DU=true` (the default) offers
   first-name terms ("Gerne auch per Du.").

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

- **Match (score ≥ `MATCH_THRESHOLD`)** — posts the full listing (all facts, reasons,
  gaps, risks) including the **📝 Bewerben** button, so the apply flow starts exactly
  as if the scanner had found it. The verdict already sits in a thread, so it stays
  one message instead of being split like a scan match.
- **No match** — posts the verdict with the failed hard rule (matched blacklist
  term) or the LLM's score, reasons, and gaps, so you see *why* it doesn't fit.
- **Files and screenshots** — upload a PDF, text file, or **image** and press
  **🔍 Check** on the prompt (see above). A screenshot goes to the vision LLM
  directly, and a passing check still offers the **📝 Bewerben** button, which drafts
  from the same screenshot.

Like `/apply`, the command posts one channel line (`🔍 Check: …`) and the verdict
lands in its thread; a checked upload is answered in the upload's own thread. That
line names the **listing**, not the first words of what you pasted: the headline is
read out of the text (a `Position:`/`Projekt:` line counts), and a recruiter mail
that only opens with "Hallo," is titled by the model instead. The channel line is
relabelled with that title once the verdict is in.

A pasted description also gets rendered **in full** — a scan match shortens it
behind its 🔗 View project button, but pasted text and uploads have no such link,
so the whole description is shown, split across sections.

A check is read-only: nothing is stored, the freshness gate is skipped, and the
scanner's watermark stays untouched. One caveat for screenshots: the hard rules read
text, so an image with no caption skips stage 2 and is judged by the LLM alone —
a caption is still rule-checked as usual.

## Deploying

Pushing to `main` deploys to the VPS: GitHub Actions runs the quality gate, builds
the image into GHCR, and the server pulls it over SSH. The server holds no
configuration of its own — the app's `.env` is rendered from the secrets of the
`prod` environment and written on every deploy. Setup, secrets, and rollback are in
[`docs/deployment.md`](docs/deployment.md).

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
