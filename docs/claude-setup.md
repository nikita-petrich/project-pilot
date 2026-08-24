# Match notification and the Claude surface

How a match reaches Nik's phone and how one tap turns it into a working Claude
session. Two independent pieces, wired by one link:

1. **ntfy** delivers the push — the worker's own HTTP POST, retried, seconds
   after the verdict.
2. **A Claude project** is where the match is handled — one chat per match,
   with the account skills and the project-pilot MCP tools.

```
Match → ntfy push  ⭐ 95 · Backend/REST-API Dev · One Day Ahead GmbH
        body   → /check-project 42  +  the match card
        click  → CLAUDE_PROJECT_URL (the project holding the match chats)
        ↓ tap
    new chat in that project: type the command from the push
        ↓ skills + MCP tools: check, draft, revise, send
```

## Why the push comes from the worker

The previous channel opened a Claude session per match and relied on the Claude
app's completion push. That push is a **per-run model decision** — the account
setting reads "Claude *can choose* to notify you" — and it dropped
notifications in practice. Anthropic closed both matching issues
([#60005](https://github.com/anthropics/claude-code/issues/60005),
[#60208](https://github.com/anthropics/claude-code/issues/60208)) as *not
planned*.

So delivery moved into code, where it can be guaranteed and retried, and Claude
kept the part it is good at: doing the work once Nik taps.

It is also faster. The push leaves the worker the moment the verdict is stored;
the old channel waited for a whole Claude run to finish first.

## Setup

### 1. ntfy

1. Install the app: [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) ·
   [iOS](https://apps.apple.com/us/app/ntfy/id1625396347). Optional on the
   desktop: subscribe to the same topic at [ntfy.sh/app](https://ntfy.sh/app).
2. Subscribe to a topic with an **unguessable** name, e.g.
   `project-pilot-a8f3k2m9x`. A topic name is the only access control on
   ntfy.sh: anyone who knows it can read the pushes. They carry score, title,
   company and the deep link — no profile text and no credentials — and the
   link is useless without Nik's Claude login.
3. Set `NTFY_TOPIC_URL=https://ntfy.sh/<topic>` in the `prod` GitHub
   environment. The deploy refuses to render an `.env` without it, and rejects
   a bare server address with no topic.

Self-hosting later (`ntfy.<domain>` behind the existing reverse proxy) changes
nothing but the value of `NTFY_TOPIC_URL`, plus `NTFY_TOKEN` if the instance is
protected.

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

### 4. The account skills

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
| `list-matches` | recent matches from the feed, with listing ids |

There is no API for uploading account skills — the dialog is the only way. They
are thin pointers at the MCP tools, so they need re-uploading only when a
skill's own wording changes, not when a rule changes.

## Working a match

1. The push arrives. The title is the whole overview: `⭐ 95 · Rolle · Firma`.
2. Tap it. The match project opens; start a chat and type the command the push
   body already names: `/check-project 42`.
3. Work the match in that chat: check, draft, revise, set the recipient, send.
   `send_application` is guarded by the skill's confirmation step and by the
   pipeline's own status guard against double sends.
4. The chat stays in the project, so the whole run is reproducible later — and
   extensible, when a new skill (interview prep, follow-up) joins the set.

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

Three steps must pass; the last one is the push. A match pushes its card, a
no-match pushes a warning — either way the channel is proven.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No push at all | Topic mismatch, or app not subscribed | Compare `NTFY_TOPIC_URL` with the topic in the app, letter for letter |
| Push arrives, tap does nothing | Claude app not installed | The link opens in the browser; log in there, or install the app |
| Tap opens the listing, not Claude | `CLAUDE_PROJECT_URL` unset | Set it to the project URL and redeploy |
| Chat has no `/check-project` | Account skills not uploaded or disabled | Upload the zips, then toggle each skill on |
| Session has no `project_pilot_*` tools | Connector missing or token rotated | Re-add the connector URL with the current `MCP_TOKEN` |
| `test-match` fails at `push` | Bad topic or unreachable server | The log names the HTTP status; a 4xx is config, a 5xx is retried |
| Deploy refuses to render `.env` | `NTFY_TOPIC_URL` missing or malformed | The gate prints the expected shape |
