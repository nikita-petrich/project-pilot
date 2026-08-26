# Match notification and the Claude surface

How a match reaches Nik's phone and how one tap turns it into a working Claude
session. Two independent pieces, wired by one link:

1. **Telegram** delivers the push — the worker's own HTTP POST, retried,
   seconds after the verdict.
2. **A Claude project** is where the match is handled — one chat per match,
   with the account skills and the project-pilot MCP tools.

```
Match → new forum topic  ⭐ 95 · Backend/REST-API Dev · One Day Ahead GmbH
        card inside that topic
        ↓ you type in the topic
    the agent answers there: checks, drafts, revises, sends
        ↓ done
    close the topic: out of the list, kept and reopenable
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

### 1b. The match supergroup

Every match opens its own **forum topic**, so one project is one thread and a
finished one can be closed rather than deleted. Topics only exist in a forum
supergroup, so the bot sends there rather than into your private chat:

1. In Telegram: **New Group** → name it (e.g. *project-pilot*) → add your bot as
   the only other member → create.
2. Open the group → **Edit** → turn on **Topics**. Telegram converts it to a
   forum supergroup.
3. **Edit → Administrators → add your bot**, and give it **Manage Topics**.
   Without that one right it cannot open a topic, and every match would land in
   the group's general area instead.
4. Read the group's id: post any message in the group, then

   ```sh
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"chat":{"id":-[0-9]*'
   ```

   It is negative and starts with `-100`. That is `TELEGRAM_CHAT_ID` in the
   `prod` environment — the deploy rejects a positive one, because a personal
   chat can never hold a topic.

The bot only ever sends. There is no polling loop, no webhook and no inbound
port, so nothing here can be reached from outside.

Install the Telegram **desktop app** as well and let it start with the system:
that is what makes a match notify you at the desk with nothing open — the
reason this channel beat a push service, whose browser delivery needs a running
browser and lapses after a week of inactivity.

### 2. Privacy mode — the bot has to be allowed to read the topic

A bot in a group runs with **privacy mode** on by default, and then only ever
receives commands, replies to its own messages, and service messages. Ordinary
text is never delivered to it, which looks exactly like a bot that ignores you:
topics still open, cards still arrive, and nothing you write gets an answer.

Telegram exempts a bot that was *added to the group as an admin*. Promoting it
afterwards does not reliably count, so make it explicit:

1. [@BotFather](https://t.me/BotFather) → `/setprivacy` → your bot → **Disable**
2. Remove the bot from the group and add it back — the setting only takes
   effect on a fresh join.
3. Give it **Manage Topics** again; that right is what lets it open a topic per
   match.

The `/` menu the bot publishes at startup is the fallback either way: a command
reaches a bot even with privacy mode on.

### 3. The MCP connector

The session needs the project-pilot tools. Add the custom connector once at
[claude.ai/customize/connectors](https://claude.ai/customize/connectors):

- URL: `https://mcp-project-pilot.sequenz.io/t/<MCP_TOKEN>/mcp`
- The token rides in the path because the connector dialog takes a URL and no
  headers. The reverse proxy therefore runs with `access_log off;` for that
  host — otherwise every call would write the token into the proxy log.

Rotating `MCP_TOKEN` means: new value in the `prod` environment, redeploy, then
update the connector URL.

### 4. The thread agent

The bot answers inside the match topics. Three secrets in the `prod`
environment:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com — billed per token, separate from any Claude subscription |
| `TELEGRAM_ALLOWED_USER_IDS` | your Telegram user id (from @userinfobot); anyone else is ignored |

Optional: `AGENT_MODEL` (default `claude-opus-5`).

Where it answers: **everywhere in the group**. A topic project-pilot opened is
about its match. A topic you open yourself is about whatever you bring into it —
paste a description, a link or a PDF and the agent stores it with
`ingest_listing` first, then works with the listing id it gets back. The
group's General area is a conversation of its own the same way. Each keeps its
own session, so three threads are three separate conversations.

What the agent is:

- **A full Claude Code agent**, running on the Claude Agent SDK inside the bot
  container. Shell, filesystem, file search and the web are all available.
- **With the same permission gate a Claude session has.** Reading and searching
  run without asking — `Read`, `Glob`, `Grep`, `WebSearch`, `WebFetch`, and the
  MCP tools that only look at a listing or produce an unsent draft. Everything
  else — `Bash`, `Write`, `Edit`, naming a recipient, sending — puts a question
  in the thread with **✅ Erlauben / 🚫 Ablehnen** and waits for your press. No
  answer within ten minutes is a refusal, and so is a question Telegram would
  not deliver. The question is rewritten into its answer afterwards, so the
  thread reads as a record instead of leaving live buttons on a settled
  decision. The list of pre-approved tools is `ALLOWED_TOOLS` in
  `src/project_pilot/agent.py` — one place, move a tool in or out.
- **With project-pilot's MCP server attached as its domain layer**, reached at
  `http://mcp:8765/mcp` inside the stack with `MCP_TOKEN` as a bearer header.
  The agent runs the MCP client itself, so nothing about a match thread goes out
  through the public hostname, and there is no second copy of the token in a
  URL. The profile,
  the judging rules and the writing style live behind those tools, so the system
  prompt sends every question about Nik or a listing through them instead of the
  model's memory. The `.claude/` directory of the image is *not* loaded
  (`setting_sources=[]`): that holds the build workflow, which has no business
  in a match thread.
- **The `/` menu is the MCP prompt list.** `check_project`, `write_application`,
  `send_application`, `enrich_company` — the bot publishes them at startup from
  `mcp_prompts.py`, and a press hands the agent that prompt's own body with the
  topic's listing filled in. No second definition to maintain, and the same
  procedure runs whether it was started here, in Claude Code, or from n8n.
- **Sending is gated twice**: the button, and an explicit yes in the
  conversation before the agent is allowed to reach for the tool at all — on top
  of the pipeline's own guard against double sends. The prompt forbids any other
  delivery route, which matters now that the agent has a shell.

While a turn runs, the thread says so rather than going quiet: your message
gets a 👀 reaction the moment it is picked up, the typing indicator is renewed
every four seconds (Telegram drops it after five), and one status line names the
step the agent is on — `⏳ prüfe das Listing gegen dein Profil …` — edited in
place and removed when the answer arrives. The reaction turns 👍 when the turn
finished, and is cleared when it failed.

Two operational details:

- The agent works in `/data/workspace` and the SDK writes each topic's
  transcript to `/data/claude`, both on the `agentdata` volume. A deploy
  replaces the container without dropping a session or the files it wrote. Only
  the session id per topic lives in Postgres; if a transcript is ever gone, the
  next message silently starts a fresh session.
- The SDK bundles its own Claude Code binary (~340 MB), so the image is that
  much larger and needs no Node.js.

Cost: judging and drafting still run on your own server against OpenAI; per
message the agent is capped at 60 turns and $5, so a runaway loop stops itself.

Turn off the bot at any time by scaling its service to zero — matches keep
arriving, only the answering stops.

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
# On the VPS — the server holds no source tree and no uv, only containers:
cd /opt/stacks/project-pilot
docker compose exec app project-pilot test-match

# Locally, in a checkout:
uv run project-pilot test-match          # rules + LLM + a real push, stores nothing
```

Three steps must pass; the last one is the notification. A match opens its
topic, a no-match sends a warning — either way the channel is proven.

Then work the topic: the card carries every listing fact and the verdict under
three buttons.

| Button | What it does |
|---|---|
| ✅ Annehmen | starts the drafting workflow in the topic |
| 🚫 Ablehnen | takes the buttons off and closes the topic (closed, not deleted) |
| 📄 Projektbeschreibung | posts the listing's own text, which the card leaves out |

Writing works the same way, in your own words or through the `/` menu. The
check and the draft run straight through; asking it to send brings up the 🔐
approval buttons.

```bash
docker compose logs -f bot               # what the agent did, and who pressed what
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No message at all | Wrong chat id, or the bot was never messaged first | Message the bot, re-read the id from `getUpdates` |
| `401 Unauthorized` in the log | Token revoked or mistyped | Regenerate with @BotFather, update the secret, redeploy |
| Arrives on the phone, not at the desk | Telegram desktop not installed or not autostarting | Install it and let it start with the system |
| Nothing you write gets an answer, `/pruefen` does | Privacy mode still on | @BotFather → `/setprivacy` → Disable, then re-add the bot to the group |
| Matches land in the group root, no topic | Bot lacks **Manage Topics**, or the group is not a forum | Turn on Topics, make the bot an admin with that right |
| Deploy rejects the chat id | A personal chat id (positive) | Use the supergroup id, negative, starting with `-100` |
| The agent never answers in a topic | The `bot` container is down, or your id is not in `TELEGRAM_ALLOWED_USER_IDS` | `docker compose logs bot`; a refused message is logged with the id that sent it |
| A 🔐 question never resolves | Pressed from outside the whitelist, or left for over ten minutes | Both count as a refusal by design; the agent asks again on the next attempt |
| `uv: command not found` on the VPS | The server has no source tree and no uv, by design | `docker compose exec app project-pilot <command>` |
| Session has no `project_pilot_*` tools | Connector missing or token rotated | Re-add the connector URL with the current `MCP_TOKEN` |
| `test-match` fails at `push` | Bad token or chat id | The log names the HTTP status; a 4xx is config, a 5xx is retried |
| Deploy refuses to render `.env` | `TELEGRAM_*` missing or malformed | The gate prints what it expected |
