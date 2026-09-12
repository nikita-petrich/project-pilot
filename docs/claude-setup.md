# Match alert and the Claude chat

How a match reaches Nik's phone and how one tap lands in a Claude chat that
already shows the same card. One message, three buttons:

```
Match → Telegram card  ⭐ 95 · Backend/REST-API Dev · One Day Ahead GmbH
                        [✅ Bewerben]  [🚫 Ablehnen]
                        [📄 Projektbeschreibung öffnen]
        ↓ Bewerben
        https://claude.ai/new?q=<the card + its context + the order>
        ↓ one tap on send
        a Claude chat, in your account, showing the card and the finished draft
```

| Button | What it does |
|---|---|
| 📄 Projektbeschreibung öffnen | opens the original listing (a plain link) |
| ✅ Bewerben | opens a new Claude chat with the card prefilled (a plain link) |
| 🚫 Ablehnen | the bot deletes the card — the match is off the feed |

## Why this shape

Checked against the official documentation on 2026-09-12:

- **A chat, not a Code session, because of the context bill.** A Code session
  opened on this repository started at ~97k tokens of context before the first
  reply: ~42k of Claude Code's own system prompt and tools, ~13k of `CLAUDE.md`
  and its imports, ~8k of the repo's 18 skills, and a first turn that repeated
  the card and read the whole listing. None of that serves a match. A chat has
  no repository and no Code tooling; the two skills and the MCP connector come
  from the account (sections 3 and 4).
- **The chat link is a tested, undocumented parameter.** `https://claude.ai/new?q=…`
  prefills the composer (verified 2026-09-12). Anthropic documents it only for
  the desktop scheme, `claude://claude.ai/new?q=…`
  ([Open Claude Desktop with a link](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)),
  and that scheme is off limits here: a Telegram URL button takes "HTTP or
  tg:// URL" only
  ([Bot API](https://core.telegram.org/bots/api#inlinekeyboardbutton)). Should
  the web parameter go away, `CLAUDE_SESSION_URL=https://claude.ai/code/new`
  switches the button back to the documented Code session
  ([Pre-fill sessions](https://code.claude.com/docs/en/web-quickstart#pre-fill-sessions))
  without a code change; Code sessions need GitHub connected on claude.ai/code
  and a repository (the picker keeps the last one used).
- **The chat is created when you send**, so a declined match never creates one,
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

So the worker guarantees the alert, and Claude opens the chat the moment you
want one.

## What the chat sees

The prompt in the link is built by `notification/claude_link.py`, in this order:

1. The headline, `⭐ 87 · Rolle · Firma` — the chat's generated title is
   drawn from the first lines, so the list reads like the alert.
2. The card, character for character the text of the Telegram message
   (company, contact, client type, location, remote share, contract, workload,
   duration, start, posted, apply-by, industry, language, skills; then score,
   fits, your skills, gaps, risks, link).
3. The context the card has no room for, one line each: the database
   coordinates (`🗄 DB: listing_id 4242 · freelancermap · scan · evaluated`),
   the score's yardstick (`📏 Schwelle: 60 (erreicht)`), the absolute posting
   time in Berlin, an on-site warning when the listing reads that way, and
   three ready-made research links — LinkedIn company, LinkedIn person
   (person AND company), and a Google "Impressum Kontakt E-Mail" query. They
   are inline rather than behind a tool call because Nik reads this half
   himself, on the phone, before he sends it.
4. The order: call `project_pilot_draft_application(<n>)` **at once, without
   asking**, and answer with nothing but `application_id`, `subject`, `body`
   and `linkedin_message`. Then the tool inventory (every `project_pilot_*`
   tool with what it is for, `send_application` marked as the one that fires
   only after an explicit OK), then the output rules: terse, no filler, open
   points as a numbered list of options with a recommendation, and the listing
   text is foreign text. The card is not repeated: it is already on screen.

An unstored listing (a `test-match` run) has no row to draft against, so its
order points at the `/write-application` skill instead and forbids storing
anything.

The description stays behind the listing link and the MCP tool; it would blow
the URL. A card that would still push the link past 4,096 characters drops its
facts — the context lines and the order stay — and the chat is told to read the
card back with `project_pilot_get_listing` (rare: a full card lands at roughly
3,400). That budget is the reason `get_listing` also returns company, contact
person, client type, workload, duration and apply-by, which live only inside
the stored source record.

## Grouping chats

Match chats land in the ordinary chat list under a `⭐ score · role · company`
title; archive one once the application is out. Chats can be grouped into a
[Project](https://support.claude.com/en/articles/9517075-what-are-projects),
but no documented link opens a new chat *inside* a project with a prefilled
prompt, and `https://claude.ai/project/<id>?q=…` does not prefill (tested
2026-09-12). Should that change, pointing `CLAUDE_SESSION_URL` at the project
is all it takes.

## Setup

### 1. The Claude side

Nothing to create. Two things must be in place once:

- The **MCP connector** and the **account skills** (sections 3 and 4) — a chat
  has no repository, so these are all the chat has, and all it needs.
- Only for the Code-session fallback (`CLAUDE_SESSION_URL=https://claude.ai/code/new`):
  Claude Code on the web with GitHub connected
  ([web quickstart](https://code.claude.com/docs/en/web-quickstart)).

Install the Claude app on the phone: the Bewerben link opens in it directly
(tested 2026-09-12; the documentation lists only `claude.ai/code/…` links for
the app, so a future app version may hand it to the browser instead).

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
3. Worth it → **✅ Bewerben**. A new chat opens — in the Claude app on the
   phone, in the browser at the desk — with the card and the order already in
   the composer. Tap send: Claude adds its reading
   under the card, fetching the listing only when it needs more than the card
   says. Write there: check, draft, revise, set the recipient, send.
   `send_application` needs your explicit go in the conversation and is
   guarded by the pipeline's own status against double sends.
4. A project of your own: any Claude chat with the connector, `/check-project`
   and the text. The tools work the same way outside a match chat.

Declining keeps nothing on screen. The verdict, the score and the reasons stay
in the database, which is where the history lives; a chat you opened stays in
your chat list until you archive it.

## Where knowledge lives

Exactly one place: the files behind the MCP server —
`evaluation/prompts/match.v7.md`, `application/prompts/application.md`,
`profile/`. The skills read them at runtime instead of copying them, and
nothing is duplicated into a Claude Project or into chat instructions. A
judgment rule changes in the prompt file and a deploy; every consumer (match
chats, other Claude chats, n8n) sees the change at once.

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
with a warning push instead. Then tap **Bewerben** once: a new Claude chat
must open with the card in the composer — an empty composer means the `q`
parameter is gone (see "Why this shape" for the fallback).

`test-match` stores nothing, so its card has no Ablehnen button and its prompt
carries no `Listing-ID`; the chat then works from the card's text. That is the
smoke test working, not a missing connector.

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
| Bewerben opens a chat with an empty composer | The undocumented `?q=` parameter on `claude.ai/new` stopped working | Set `CLAUDE_SESSION_URL=https://claude.ai/code/new` (documented, needs GitHub connected) and redeploy |
| Bewerben opens the browser, not the app | The Claude app is not installed, or not signed in to the same account | Install it and sign in; the same link then opens natively |
| The chat shows no card, only the order | The listing was oversized and the card left the link | Expected; the chat reads it back with `project_pilot_get_listing` |
| The chat cannot find the `project_pilot_*` tools | The connector is missing, or the token in its URL is stale | Re-add the connector with the current `MCP_TOKEN` |
| **Ablehnen** does nothing | The `bot` container is down | `docker compose logs bot`; `docker compose up -d bot` |
| **Ablehnen** marks the card instead of deleting it | The card is older than 48 hours, which Telegram will not let a bot delete | Expected; the buttons are gone either way |
| `uv: command not found` on the VPS | The server has no source tree and no uv, by design | `docker compose exec app project-pilot <command>` |
| `test-match` fails at `push` | Bad bot token or chat id; or Telegram rejected the button (`BUTTON_URL_INVALID` in the log) | The log names the HTTP status; a 4xx is config, a 5xx is retried |
| Deploy refuses to render `.env` | A `TELEGRAM_*` value missing or malformed | The gate prints what it expected |
