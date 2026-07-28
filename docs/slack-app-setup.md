# Slack app setup (bot creation blueprint)

This document is the reproducible blueprint for creating — or **re-creating** — the
Slack app that powers project-pilot. If the app is ever deleted or you move to a new
workspace, follow these steps and you get an identical bot back.

project-pilot talks to Slack over **Socket Mode**: the app dials out to Slack, so it
needs **no public URL** and runs happily behind NAT on a home server. Socket Mode is
available on Slack's free tier. Everything the app needs — the bot user, the `/apply`
and `/check` slash commands, the required scopes, event subscriptions, and
interactivity — is declared once in the app manifest below, so nothing gets forgotten.

All bot output (Slack messages and logs) is in English. The only text that follows
the project's language is the generated application e-mail itself: a German project
description produces a German application, an English one produces an English
application.

## 1. Create the app from the manifest

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** →
   **From a manifest**.
2. Pick your workspace → **Next**.
3. Switch the editor to **YAML**, replace the content with the manifest below, then
   **Next → Create**.

```yaml
display_information:
  name: project-pilot
  description: Freelance project alerts & application autopilot
features:
  bot_user:
    display_name: project-pilot
    always_online: true
  slash_commands:
    - command: /apply
      description: Create an application from a link or project description
      usage_hint: "<freelancermap link or project description>"
      should_escape: false
    - command: /check
      description: Check a listing against your profile (hard rules + LLM match)
      usage_hint: "<freelancermap link or project description>"
      should_escape: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - commands
      - channels:history
      - files:read
settings:
  event_subscriptions:
    bot_events:
      - message.channels
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
  org_deploy_enabled: false
  token_rotation_enabled: false
```

> Using a **private** channel? Swap `channels:history` → `groups:history` and
> `message.channels` → `message.groups` in the manifest.

## 2. App-level token (Socket Mode) → `SLACK_APP_TOKEN`

**Basic Information → App-Level Tokens → Generate Token and Scopes.** Add the scope
`connections:write` and generate it. The token starts with `xapp-…`.

## 3. Install the app → `SLACK_BOT_TOKEN`

**Install App → Install to Workspace → Allow.** Copy the **Bot User OAuth Token**
(`xoxb-…`). Re-installing is also how new scopes or slash commands become active
after a manifest change.

## 4. Channel → `SLACK_CHANNEL`

Create or open the channel, invite the bot with `/invite @project-pilot`, then copy
the **channel ID** (`C…`, shown at the bottom of the channel details). Use the ID,
not the `#name`. The bot only serves this one channel.

## Scopes at a glance

| Scope | Why |
| --- | --- |
| `chat:write` | post and update match/draft/status messages |
| `commands` | receive the `/apply` and `/check` slash commands |
| `channels:history` | read thread replies (revisions, recipient address, screenshots) |
| `files:read` | download an uploaded PDF/text/image file to draft from or check it |
| `connections:write` (app-level) | open the Socket Mode connection |

## Environment and running

Put the three values into `.env` (see `.env.example` for the full list, including the
optional `CV_DE_PATH` / `CV_EN_PATH` for CV attachments):

```bash
SLACK_BOT_TOKEN=xoxb-…
SLACK_APP_TOKEN=xapp-…
SLACK_CHANNEL=C0123456789
```

Then:

```bash
uv run project-pilot test-notify   # posts a test message (verifies token + channel)
uv run project-pilot bot           # runs only the Slack bot
uv run project-pilot daemon        # runs the 15-min scanner + the Slack bot together
```

A successful bot start logs `slack bot started (socket mode)`.

## Re-creating the app later

Because the whole configuration lives in the manifest above, re-creating the bot is
just step 1–4 again with the same YAML. Delete the old app first (**Basic
Information → Delete App**) so two bots don't post into the same channel, then create
a fresh one from the manifest and drop the new tokens into `.env`.
