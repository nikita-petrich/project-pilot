# Match notification and the Claude surface

How a match reaches Nik's phone and how one tap turns it into a working Claude
session. Two independent pieces, wired by one link:

1. **Telegram** delivers the push — the worker's own HTTP POST, retried,
   seconds after the verdict.
2. **A Claude project** is where the match is handled — one chat per match,
   with the account skills and the project-pilot MCP tools.

```
Match → one card in Telegram: the whole listing, facts, verdict, description
        [🔗 Projekt öffnen]  [✅ Annehmen]  [🗑 Abnehmen]
           │                    │              └─ card is deleted, done
           │                    └─ application is drafted right away,
           │                       card becomes [💬 In Claude öffnen]
           └─ the listing on its own board
        ↓ tap "In Claude öffnen"
    the match project: /write-application 42 → review, revise, send
        ↓ skills + MCP tools: check, draft, revise, send
```

## Why the notification comes from the worker

The previous channel opened a Claude session per match and relied on the Claude
app's completion push. That push is a **per-run model decision** — the account
setting reads "Claude *can choose* to notify you" — and it dropped
notifications in practice. Anthropic closed both matching issues
([#60005](https://github.com/anthropics/claude-code/issues/60005),
[#60208](https://github.com/anthropics/claude-code/issues/60208)) as *not
planned*.

So delivery moved into code, where it can be guaranteed and retried, and Claude
kept the part it is good at: doing the work once Nik taps.

It is also faster. The message leaves the worker the moment the verdict is
stored; the old channel waited for a whole Claude run to finish first.

## Setup

### 1. The Telegram bot

1. Open [@BotFather](https://t.me/BotFather) in Telegram, send `/newbot`, give
   it a name and a username ending in `bot`. Copy the token it returns — that
   is `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message (a bot cannot open a chat on its own), then
   read the chat id:

   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
     | grep -o '"chat":{"id":[-0-9]*' | head -1
   ```

   That number is `TELEGRAM_CHAT_ID`.
3. Put the token in the `prod` GitHub environment as `TELEGRAM_BOT_TOKEN`. The
   deploy refuses to render an `.env` without it, and rejects a value that is
   not shaped like a token (the usual mix-up is pasting the chat id or the
   bot's `@name`).

### 1b. Where the cards land

`TELEGRAM_CHAT_ID` is any chat the bot can write to: your private chat with it,
or a group holding only the two of you. A group keeps the match feed out of the
personal chat list and is what this setup uses:

1. In Telegram: **New Group** → name it (e.g. *project-pilot*) → add your bot as
   the only other member → create.
2. Read the group's id: post any message in the group, then

   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":-\?[0-9]*'
   ```

   That is `TELEGRAM_CHAT_ID` in the `prod` environment. For a group it is
   negative (usually starting `-100`); for a private chat it is your own user
   id. The deploy rejects anything that is not an integer, so an @name or an
   invite link fails at deploy rather than at the first match.

The worker only ever sends. Button presses are read by a separate process
(section 4) over long polling, so there is still no webhook and no inbound port.

Install the Telegram **desktop app** as well and let it start with the system:
that is what makes a match notify you at the desk with nothing open — the
reason this channel beat a push service, whose browser delivery needs a running
browser and lapses after a week of inactivity.

### 2. The Claude project

Create one project on claude.ai that collects the match chats, and put its URL
in `CLAUDE_PROJECT_URL` (`https://claude.ai/cowork/project/<id>`). That keeps
match work out of the everyday chat list without a second surface.

Leave its instructions **empty**. Profile, judging rules and writing rules live
behind the MCP server and are read at runtime; copying any of them into project
instructions would create a second copy to maintain and would bind the workflow
to this one project, while n8n and other consumers use the same tools.

Without the setting a tapped push opens the listing on its own board instead —
useful, but no work surface.

### 3. The MCP connector

The session needs the project-pilot tools. Add the custom connector once at
[claude.ai/customize/connectors](https://claude.ai/customize/connectors):

- URL: `https://mcp-project-pilot.sequenz.io/t/<MCP_TOKEN>/mcp`
- The token rides in the path because the connector dialog takes a URL and no
  headers. The reverse proxy therefore runs with `access_log off;` for that
  host — otherwise every call would write the token into the proxy log.

Rotating `MCP_TOKEN` means: new value in the `prod` environment, redeploy, then
update the connector URL.

### 4. The button handler

A second process (`project-pilot telegram-bot`) watches the cards' buttons. It
holds no conversation and answers no messages — it only long-polls for button
presses, so the worker still publishes no port.

| Secret | Value |
|---|---|
| `TELEGRAM_ALLOWED_USER_IDS` | your Telegram user id (from @userinfobot). Anyone else's press is refused — those buttons write applications |
| `CLAUDE_PROJECT_URL` | the Claude project the accepted card points at |

What the buttons do:

- **🔗 Projekt öffnen** — a plain link to the listing on its board. No code.
- **✅ Annehmen** — drafts the application immediately, through the same
  `ApplicationService` the MCP tools use, so there is one drafting path rather
  than two. The card is then rewritten to name the application id and the
  commands to type, with a single button into the Claude project. **It never
  sends** — the bot process is wired without a mailer at all, so sending is
  impossible from here even by accident.
- **🗑 Abnehmen** — deletes the card. The database keeps the record either way,
  so the feed stays clean without losing the history.

The listing id travels inside every button (`accept:42`), never resolved
against some "current" listing, so two cards can never be confused for one
another.

### 5. The account skills

Repository skills load in a session that checks out the repo, but the web slash
menu does not list them. The account skills do appear in `/`, in every chat and
cloud session. Upload the five folders under `deploy/claudeai-skills/` (zipped,
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

The same four procedures are also exposed by the MCP server itself as **MCP
prompts** (`src/project_pilot/mcp_prompts.py`). Claude Code lists those as
`/mcp__project-pilot__check_project` and friends, and any bot can build its own
command menu from `prompts/list` — one definition, every surface. If the
connector surfaces them in the Claude app too, the uploaded account skills
become redundant and can be turned off; that is worth checking once with `/` in
a chat that has the connector.

## Working a match

1. A new topic appears in the group, named `⭐ 95 · Rolle · Firma`, with the
   card inside. The notification reaches phone and desktop.
2. Tap the button. The match project opens; start a chat and type the command
   the message already names: `/check-project 42`.
3. Work the match in that chat: check, draft, revise, set the recipient, send.
   `send_application` is guarded by the skill's confirmation step and by the
   pipeline's own status guard against double sends.
4. When you are done, **close the topic** (long-press → Close). It leaves the
   active list and stays fully readable and reopenable, and the Claude chat
   stays in the project — so the whole run is reproducible later.

Match chats live in their own project, so they never mix with everyday chats.

Nothing is prefilled into the composer: claude.ai removed URL prompt prefill for
chats in October 2025 (prompt-injection risk), and it exists only for Code
sessions. Hence the command in the push body.

## Where knowledge lives

Exactly one place: the files behind the MCP server —
`evaluation/prompts/match.v7.md`, `application/prompts/application.md`,
`profile/`. The skills read them at runtime instead of copying them, and
nothing is duplicated into a Claude Project or into session instructions. A
judgment rule changes in the prompt file and a deploy; every consumer (Claude
chats, cloud sessions, n8n) sees the change at once.

## Verify

```bash
uv run project-pilot test-match          # rules + LLM + a real push, stores nothing
```

Three steps must pass; the last one is the notification. A match sends its
card, a no-match sends a warning — either way the channel is proven.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No message at all | Wrong chat id, or the bot was never messaged first | Message the bot, re-read the id from `getUpdates` |
| `401 Unauthorized` in the log | Token revoked or mistyped | Regenerate with @BotFather, update the secret, redeploy |
| Arrives on the phone, not at the desk | Telegram desktop not installed or not autostarting | Install it and let it start with the system |
| Annehmen/Abnehmen do nothing | The `bot` container is down, or your id is not in `TELEGRAM_ALLOWED_USER_IDS` | `docker compose logs bot`; the refusal is logged with the id that pressed |
| Annehmen answers, but the Claude button is missing | `CLAUDE_PROJECT_URL` unset | Set it to the project URL and redeploy |
| Claude button opens the browser, not the app | Claude app not installed | Log in there, or install the app |
| Deploy rejects the chat id | An @name or an invite link instead of the id | Read the integer from `getUpdates` |
| Chat has no `/check-project` | Account skills not uploaded or disabled | Upload the zips, then toggle each skill on |
| Session has no `project_pilot_*` tools | Connector missing or token rotated | Re-add the connector URL with the current `MCP_TOKEN` |
| `test-match` fails at `push` | Bad token or chat id | The log names the HTTP status; a 4xx is config, a 5xx is retried |
| Deploy refuses to render `.env` | `TELEGRAM_*` missing or malformed | The gate prints what it expected |
