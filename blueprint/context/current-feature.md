# Current Feature

> **Generated file.** Holds the one feature or fix being built right now. Run
> `/feature <number-or-name>` to spec a build-plan feature, or `/fix "<bug>"` for
> an ad-hoc fix. Build one thing at a time; `/complete` archives it (to
> `blueprint/history/features/` or `blueprint/history/fixes/`) and resets this file.

## Feature 16: Bewerbungs-Autopilot (Apply-Button, E-Mail-Versand, LinkedIn-Nachricht)

**Goal.** From a Telegram match message, one tap on an **Apply** button produces a
personalized, concise application (LLM-written from `profile/profile.md`), shows it
for review, lets Nik iterate by replying to the draft message, and only sends the
e-mail through his own SMTP server after an explicit **Send** button tap. Every
draft also ships a copy-pastable LinkedIn message (max 250 chars). An `/apply
<link-or-description>` bot command starts the same flow for arbitrary listings.

**Out of scope.** No auto-send without review, no LinkedIn API automation, no
attachment/CV handling, no multi-user support. The application prompt is one
single file (`application.md`) holding Nik's own bid-writing prompt.

### Flow (binding)

1. Match messages carry an inline **📝 Bewerben** button (`apply:<listing_id>`).
2. Apply tap → LLM drafts subject + e-mail body + LinkedIn message from the
   profile and the stored listing. The recipient e-mail is auto-extracted from the
   listing (description/raw); if none is found the bot asks for it and the draft
   waits in `awaiting_email`.
3. The draft is always shown first (recipient, subject, body, LinkedIn text) with
   **📤 Senden** (only when a recipient is set) and **❌ Verwerfen** buttons.
4. Replying to a draft message with plain text revises the draft via the LLM;
   replying with a bare e-mail address sets/replaces the recipient.
5. Only the Send tap delivers the e-mail via SMTP (`aiosmtplib`, direct to Nik's
   mail server). Success marks the application `sent`; double-taps are guarded by
   status. Send failures keep the draft and report the error.
6. `/apply <freelancermap-url>` reuses a stored listing (url_hash) or fetches and
   parses the page; `/apply <free text>` treats the text as the project
   description. Both enter the same review flow.

### Steps

- [x] 1. Config & errors: SMTP settings (`SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS`)
  with `require_smtp()`, `.env.example` updated; new domain errors
  (`EmailSendError`, `ApplicationStateError`); dependency `aiosmtplib`.
- [x] 2. Data model: `applications` table (status enum `awaiting_email | ready |
  sent | cancelled`, draft fields, LinkedIn message, telegram draft message id,
  token/model/prompt metadata) + Alembic migration + repository methods.
- [x] 3. Application module: `ApplicationDraft` schema (LinkedIn ≤ 250 chars),
  single prompt file `application/prompts/application.md` (Nik's bid-writing
  prompt), `ApplicationGenerator` (generate + revise, one retry, `LlmSchemaError`
  on failure), `SmtpMailer`.
- [x] 4. `ApplicationService`: draft-for-listing / draft-from-text / revise /
  set-recipient / send / cancel with status guards, e-mail auto-extraction, and
  persistence (one session per interaction).
- [x] 5. Telegram: `send()` with inline keyboards returning message ids,
  `get_updates` long polling, `answer_callback`, update schemas, draft message
  formatting, apply/send/cancel keyboards; pipeline match messages now use the
  apply button (`send_match`).
- [x] 6. Bot: `TelegramBot` long-poll loop routing callbacks (`apply/send/cancel`),
  `/apply` command (url or text), and draft replies (revision vs. recipient
  e-mail); only the configured chat is served; daemon runs scanner + bot
  concurrently; new `bot` CLI command.
- [x] 7. Tests: generator (retry/fallback), schema cap, mailer (fake transport),
  service flow incl. guards (Postgres-backed), bot routing (fake client/service),
  telegram client additions (respx), config, pipeline notifier update.
- [x] 8. Docs: README section, build-plan entry 16.

### Done when

- A match message shows the Apply button; tapping it yields a draft message with
  subject, concise body, LinkedIn text (≤ 250 chars) and the review buttons.
- A missing recipient is asked for and can be supplied by replying with the
  address; replies with instructions change the draft.
- Send delivers via SMTP exactly once (status-guarded) and confirms in Telegram;
  failures are reported and retryable.
- `/apply` works with a freelancermap link and with pasted description text.
- Quality gate green: `uv run ruff check`, `uv run ruff format --check`,
  `uv run mypy`, `uv run pytest`.
