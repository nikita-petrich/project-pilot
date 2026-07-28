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
  no-gos, and reference projects. **This file is gitignored and stays local** (it
  holds personal CV/contact data) — a sanitized `profile/profile.example.md`
  template is tracked instead, like `.env.example`.
- `profile/constraints.yaml` hard rules (blacklist terms, optional must-have)

Optionally add an e-mail signature (see [E-mail signature](#e-mail-signature)); the
contact block under the greeting comes from there, not from `profile.md`.

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
| `SIGNATURE_DIR` | optional directory holding the e-mail signature (see below) |

## Commands

```sh
uv run project-pilot init-db        # apply Alembic migrations
uv run project-pilot run-once       # one scan now (non-zero exit on a failed run)
uv run project-pilot daemon         # scheduler + Slack bot until SIGTERM
uv run project-pilot bot            # only the Slack bot (no scanning)
uv run project-pilot test-notify    # post a test message to the Slack channel
uv run project-pilot stats          # reporting summary
uv run project-pilot healthcheck    # liveness/freshness probe (exit code)
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
- **Signature** — appended on send, in the draft's language (see below). The draft
  shown in Slack therefore ends at your name; the contact block is added to the
  outgoing mail, not to the text you review.

`/apply <freelancermap-link>` starts the same flow for any listing (stored or
freshly fetched), and `/apply <pasted project description>` works without a link.
**Uploading a file** (PDF or text) to the channel drafts from its contents the same
way — drop in a project-description PDF and the draft appears. Nothing is ever sent
without the explicit Send tap.

## E-mail signature

Point `SIGNATURE_DIR` at a directory holding one pair of files per language:

| File | Purpose |
|---|---|
| `signature.de.html` | the HTML signature for German drafts |
| `signature.de.txt` | plain-text fallback, **required** next to the HTML |
| `signature.en.html` / `signature.en.txt` | the same pair for English drafts |
| `photo.jpg`, `linkedin.png`, … | every image the HTML references |

Copy `profile/signature.example.html` and `profile/signature.example.txt` as a
starting point. `profile/signature/` is gitignored, so it is a good place to keep
the real one.

The HTML references images as `cid:<name>`, and `<name>` resolves to `<name>.<ext>`
in the same directory — `cid:photo` finds `photo.jpg`. **Images are embedded into
the message** (a per-message Content-ID, `multipart/related`), so they render
without the recipient allowing remote content, and they do not show up as
attachments next to the CV.

Mails are then sent as `multipart/alternative`: the plain-text part is the draft
plus `signature.<lang>.txt`, the HTML part is the draft rendered as paragraphs plus
`signature.<lang>.html`. Without `SIGNATURE_DIR` nothing changes — the mail stays
plain text and ends at your name.

Two things to know when writing the HTML:

- **Use tables and inline styles.** Outlook ignores flexbox, grid and `<style>`
  blocks; the example template sticks to what renders everywhere.
- **Skip HTML comments.** The file is sent verbatim, so a comment rides along in
  every mail — and a `cid:` reference inside one gets rewritten like any other.
- **A misconfigured signature aborts at startup**, on purpose — a missing image or
  a missing `.txt` fallback fails loudly rather than silently sending bare mail.

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

## Project layout

```
src/project_pilot/
  config.py profile_loader.py errors.py db.py models.py repository.py
  ingestion/    client, parser, normalize, watermark
  evaluation/   freshness, rules, llm, schemas, prompts/
  notification/ slack
  pipeline.py scheduler.py reporting.py cli.py
alembic/        async migrations
docs/           compliance.md, operations.md, adr/
tests/          unit + integration, fixtures/
```

Built with the [AI Coding Blueprint](blueprint/README.md); agent instructions live
in [AGENTS.md](AGENTS.md) and [CLAUDE.md](CLAUDE.md).
