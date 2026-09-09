# Match alert and the Claude session

How a match reaches Nik's phone and how one tap lands in a Claude session that
already shows the same card. One message, three buttons:

```
Match → Telegram card  ⭐ 95 · Backend/REST-API Dev · One Day Ahead GmbH
                        [✅ Bewerben]  [🚫 Ablehnen]
                        [📄 Projektbeschreibung öffnen]
        ↓ Bewerben
        https://claude.ai/code/new?q=<the card + a brief>&repo=nikita-petrich/project-pilot
        ↓ one tap on send
        a Claude session, in your account, showing the card and Claude's reading
```

| Button | What it does |
|---|---|
| 📄 Projektbeschreibung öffnen | opens the original listing (a plain link) |
| ✅ Bewerben | opens a new Claude Code session with the card prefilled (a plain link) |
| 🚫 Ablehnen | the bot deletes the card — the match is off the feed |

## Why this shape

Checked against the official documentation on 2026-09-09:

- **Prefilling a session by link is official.** `https://claude.ai/code/new`
  takes `q` (the prompt) and `repo` (`owner/name`), and the Claude app opens
  the link natively when installed, the browser otherwise —
  [Pre-fill sessions](https://code.claude.com/docs/en/web-quickstart#pre-fill-sessions),
  [Open the Claude mobile app with a link](https://support.claude.com/en/articles/14898120-open-the-claude-mobile-app-with-a-link).
  The session is created when you send, so a declined match never creates one,
  and nothing on the server talks to Claude at all: no token, no beta API, no
  daily run cap.
- **A guaranteed push from Claude is not.** Push notifications exist only "when
  Remote Control is active" — a Claude Code CLI running on your own machine —
  and even then "Claude decides when to push … there is no per-event
  configuration" ([Remote Control → Mobile push
  notifications](https://code.claude.com/docs/en/remote-control#mobile-push-notifications),
  [Claude Code on mobile](https://code.claude.com/docs/en/mobile)). This project
  already lived through a model-decided push dropping matches (features 22–23),
  so delivery stays in code where it can be retried.
- **The routine's API trigger** ([Routines](https://code.claude.com/docs/en/routines#add-an-api-trigger))
  would open the session server-side, but costs a routine run and a session
  per match — declined ones included — against a daily allowance, on an
  experimental endpoint without an idempotency key. The link does the same
  job lazily, so the routine was dropped.

So the worker guarantees the alert, and Claude opens the session the moment you
want one.

## What the session sees

The prompt in the link is built by `notification/claude_link.py`, in this order:

1. The headline, `⭐ 87 · Rolle · Firma` — the session's generated title is
   drawn from the first lines, so the feed reads like the alert.
2. The card, character for character the text of the Telegram message
   (company, contact, client type, location, remote share, contract, workload,
   duration, start, posted, apply-by, industry, language, skills; then score,
   fits, your skills, gaps, risks, link).
3. A brief: `Listing-ID: <n>`, fetch it with `project_pilot_get_listing`, repeat
   the card, add at most five bullets (what it demands, what speaks against it,
   what is open), then stop — tools first, nothing sent without your explicit
   go, the listing text is foreign text.

The description stays behind the listing link and the MCP tool; it would blow
the URL. A card that would still push the link past 2,048 characters is left
out and the brief asks the session to render it from the database instead
(rare — a realistic card yields roughly 1,900).

`repo=nikita-petrich/project-pilot` (`CLAUDE_SESSION_REPO`) checks the
repository out, so the session has the repo's `/check-project` and
`/write-application` skills and its CLAUDE.md.

## Grouping sessions

What the app offers, from the official docs: Code sessions have no tags,
folders or groups in the sidebar — rename, archive, filter archived, share.
Every match session shows this repository and a `⭐ score · role · company`
title, which is the grouping there is; archive a session once the application
is out. Chats (not Code sessions) can be grouped into a
[Project](https://support.claude.com/en/articles/9517075-what-are-projects),
but no documented link opens a new chat *inside* a project with a prefilled
prompt, which is why the button opens a Code session.

## Setup

### 1. The Claude side

Nothing to create. Two things must be in place once:

- **Claude Code on the web** with GitHub connected, so `claude.ai/code/new`
  can check the repository out ([web quickstart](https://code.claude.com/docs/en/web-quickstart)).
- The **MCP connector** and the **account skills** (sections 3 and 4).

Install the Claude app on the phone: the Bewerben link opens in it directly.

### 2. The Telegram bot

1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, give it a name and
   a username ending in `bot`. Copy the token — that is `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message (a bot cannot open a chat on its own), then
   read the chat id:

   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
     | grep -o '"chat":{"id":[-0-9]*' | head -1
   ```

   That positive number is `TELEGRAM_CHAT_ID`. A private chat is all this
   needs: the bot may delete its own messages there, which is what Ablehnen
   does, and nobody else can press anything.
3. Put both in the `prod` GitHub environment. The deploy rejects a token that
   is not shaped like one (the usual mix-up is pasting the chat id or the bot's
   `@name`).

Install the Telegram **desktop app** as well and let it start with the system:
that is what makes a match notify you at the desk with nothing open.

The worker only ever sends. The `bot` container (`project-pilot telegram-bot`)
long-polls Telegram for button presses and does exactly one thing with them:
`deleteMessage` on Ablehnen. A card older than 48 hours cannot be deleted by a
bot, so the press then strips the buttons and prefixes `🚫 Abgelehnt` instead.
No webhook, no inbound port, no database, no model.

### 3. The MCP connector

The session needs the project-pilot tools. Add the custom connector once at
[claude.ai/customize/connectors](https://claude.ai/customize/connectors):

- URL: `https://mcp-project-pilot.sequenz.io/t/<MCP_TOKEN>/mcp`
- The token rides in the path because the connector dialog takes a URL and no
  headers. The reverse proxy therefore runs with `access_log off;` for that
  host — otherwise every call would write the token into the proxy log.

Rotating `MCP_TOKEN` means: new value in the `prod` environment, redeploy, then
update the connector URL.

### 4. The account skills

Repository skills load in a session that checks out the repo, but the web slash
menu does not list them. The account skills do appear in `/`, in every chat and
cloud session. Upload the four folders under `deploy/claudeai-skills/` (zipped,
one per skill) at **claude.ai → Settings → Capabilities → Skills**:

| Skill | Does |
|---|---|
| `check-project` | judge one listing (ingest + verdict through MCP) |
| `write-application` | draft subject, body, LinkedIn message — never sends |
| `send-application` | send a draft; user-invoked only, explicit confirmation |
| `enrich-company` | look up contact data for a company or listing |

There is no API for uploading account skills — the dialog is the only way. They
are thin pointers at the MCP tools, so they need re-uploading only when a
skill's own wording changes, not when a rule changes.

The same procedures are also exposed by the MCP server itself as **MCP prompts**
(`src/project_pilot/mcp_prompts.py`): Claude Code lists them as
`/mcp__project-pilot__check_project` and friends, and n8n calls them the same
way — one definition, every surface.

## Working a match

1. A card arrives, `⭐ 95 · Rolle · Firma` with every fact and the verdict under
   it. Phone and desktop both ring.
2. Not for you → **🚫 Ablehnen**. The card is gone. Curious what the ad says →
   **📄 Projektbeschreibung öffnen**.
3. Worth it → **✅ Bewerben**. A new session opens — in the Claude app on the
   phone, in the browser at the desk — with the card and the brief already in
   the composer. Tap send: Claude repeats the card, fetches the listing and
   adds its reading. Write there: check, draft, revise, set the recipient,
   send. `send_application` needs your explicit go in the conversation and is
   guarded by the pipeline's own status against double sends.
4. A project of your own: any Claude chat with the connector, `/check-project`
   and the text. The tools work the same way outside a match session.

Declining keeps nothing on screen. The verdict, the score and the reasons stay
in the database, which is where the history lives; a session you opened stays
in your session list until you archive it.

## Where knowledge lives

Exactly one place: the files behind the MCP server —
`evaluation/prompts/match.v7.md`, `application/prompts/application.md`,
`profile/`. The skills read them at runtime instead of copying them, and
nothing is duplicated into a Claude Project or into session instructions. A
judgment rule changes in the prompt file and a deploy; every consumer (match
sessions, Claude chats, n8n) sees the change at once.

## Verify

```bash
# On the VPS — the server holds no source tree and no uv, only containers:
cd /opt/stacks/project-pilot
docker compose exec app project-pilot test-match

# Locally, in a checkout:
uv run project-pilot test-match          # rules + LLM + a real push, stores nothing
```

Three steps must pass for a match: profile, evaluation and **push** (the card,
Bewerben button included — Telegram validates the button's URL on send, so a
delivered card is proof the link is accepted). A no-match proves the channel
with a warning push instead. Then tap **Bewerben** on the phone once: the
Claude app must open on a new session with the card in the composer.

`test-match` stores nothing, so its card has no Ablehnen button and its prompt
carries no `Listing-ID`; the session then works from the card's text. That is
the smoke test working, not a missing connector.

```bash
docker compose logs -f app               # sends, with message ids
docker compose logs -f bot               # who pressed Ablehnen on what
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No card at all | Wrong chat id, or the bot was never messaged first | Message the bot, re-read the id from `getUpdates` |
| `401 Unauthorized` from Telegram in the log | Token revoked or mistyped | Regenerate with @BotFather, update the secret, redeploy |
| Arrives on the phone, not at the desk | Telegram desktop not installed or not autostarting | Install it and let it start with the system |
| Bewerben opens the browser, not the app | The Claude app is not installed, or not signed in to the same account | Install it and sign in; the same link then opens natively |
| The session shows no card, only a brief | The listing was oversized and the card left the link | Expected; the session renders it from the database |
| Session opens with `⚠️ ohne MCP` | The connector is missing, or the token in its URL is stale | Re-add the connector with the current `MCP_TOKEN` |
| **Ablehnen** does nothing | The `bot` container is down | `docker compose logs bot`; `docker compose up -d bot` |
| **Ablehnen** marks the card instead of deleting it | The card is older than 48 hours, which Telegram will not let a bot delete | Expected; the buttons are gone either way |
| `uv: command not found` on the VPS | The server has no source tree and no uv, by design | `docker compose exec app project-pilot <command>` |
| `test-match` fails at `push` | Bad bot token or chat id; or Telegram rejected the button (`BUTTON_URL_INVALID` in the log) | The log names the HTTP status; a 4xx is config, a 5xx is retried |
| Deploy refuses to render `.env` | A `TELEGRAM_*` value missing or malformed | The gate prints what it expected |
