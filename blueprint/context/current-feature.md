# Current Feature

> **Generated file.** Holds the one feature or fix being built right now. Run
> `/feature <number-or-name>` to spec a build-plan feature, or `/fix "<bug>"` for
> an ad-hoc fix. Build one thing at a time; `/complete` archives it (to
> `blueprint/history/features/` or `blueprint/history/fixes/`) and resets this file.

## Feature 17: Slack statt Telegram (Notifier & Bewerbungs-Bot)

**Goal.** Replace Telegram entirely with Slack. A match posts **one** Slack message
(Block Kit) carrying the full listing plus an **Bewerben** button. The apply flow
posts **one** message with the complete e-mail (code blocks, copyable, split across
`section` blocks so nothing is truncated), the LinkedIn message, and action buttons
**📤 Senden / ❌ Verwerfen / 📧 Im Mail-Client öffnen** (a real `mailto:` URL button —
Slack allows it, Telegram did not). Draft revisions happen by replying **in the
message's thread**; a bare e-mail in the thread sets the recipient. `/apply
<link-or-text>` is a Slack slash command. Runs behind NAT via **Socket Mode** (no
public URL), free tier.

**Out of scope.** No Telegram fallback (full replacement). No Slack Workflow/BOLT
framework — thin `slack_sdk` AsyncWebClient + SocketModeClient, explicit composition.
No new application/matching logic; only the delivery/interaction surface changes.

### Steps

- [x] 1. Config & deps: `slack_sdk` dependency; `SlackConfig` + `require_slack()`
  (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_CHANNEL`), `.env.example`; move the
  `MatchMessage` data class to `notification/messages.py` (survives Telegram removal).
- [x] 2. Slack messages: Block Kit builders in `notification/slack.py` — match
  message (fields + Bewerben button) and draft message (full e-mail split into
  ≤3000-char code sections, subject, recipient, LinkedIn, Senden/Verwerfen/mailto
  buttons); thin `SlackClient` wrapper (post/update/thread) over an injectable
  web-client protocol; unit tests (pure builders + fake client).
- [x] 3. Data model: applications draft reference becomes a Slack `channel:ts`
  string — Alembic migration `draft_message_id` (BigInteger) → `draft_ref` (String);
  repository + service updated (`record_draft_ref`, `find_by_draft_ref`).
- [x] 4. `SlackBot`: Socket Mode loop routing block-button actions (apply/send/cancel),
  the `/apply` slash command, and thread replies (revision vs. recipient e-mail);
  only the configured channel is served; posts one-message drafts; `bot` CLI; daemon
  runs scanner + Slack bot concurrently.
- [x] 5. Pipeline & CLI: notifier posts Slack blocks (`send_match` takes the
  `MatchMessage`), warnings via Slack, `test-notify` posts to Slack; wire Slack into
  `_build_pipeline`/`_build_bot`.
- [x] 6. Remove Telegram: delete `notification/telegram.py`, `notification/bot.py`,
  their tests, and the Telegram config; drop `httpx` Telegram usage.
- [x] 7. Docs: README + AGENTS.md Slack setup (app, Socket Mode, scopes, tokens,
  channel), build-plan entry 17.

### Done when

- A match posts one Slack message with all listing data and a Bewerben button.
- Bewerben (or `/apply`) posts one message with the full e-mail (never truncated),
  LinkedIn text, and Senden/Verwerfen/Mail-öffnen buttons.
- Thread reply revises; a bare e-mail in the thread sets the recipient; Senden
  delivers via SMTP exactly once; Mail-öffnen opens the client with recipient+subject.
- Runs via Socket Mode without a public URL; no Telegram code remains.
- Quality gate green: `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`, `uv run pytest`.
