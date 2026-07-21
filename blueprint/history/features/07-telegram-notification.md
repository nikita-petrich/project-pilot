# Feature 7: Telegram notification

**From build-plan:** feature 7
**Status:** done (2026-07-21)

## Goal

Deliver match alerts to Telegram: a lean Bot API client, a compact HTML message
format, a per-run digest, and a `test-notify` CLI command.

## Outcome

- `notification/telegram.py`: `TelegramClient.send_message` posts to the Bot API
  `sendMessage` with `parse_mode=HTML` and returns success as a bool (transport
  error, non-200, or `ok:false` all return False, so the pipeline can retry next
  run). `MatchMessage` + `format_match` (title link, score, up to three reasons,
  start/location/remote, HTML-escaped) and `build_digest` (one message per run,
  singular/plural header, empty string for none).
- `cli.py`: `test-notify` command (requires Telegram credentials, sends a test
  message, non-zero exit on failure) plus a group callback so subcommands work.

## Build steps

- [x] **Step 1 - Client + message formatting**
- [x] **Step 2 - test-notify command**

## Tests

`test_telegram.py` (link/score/reasons, HTML escaping, reason cap, digest
singular/plural/empty, send success, api-error, transport-error, bad-status) and
`test_cli.py` (test-notify sends with a mocked API; missing credentials exit
non-zero). 105 tests, full gate green; telegram 99%.

## Design notes and NIK-TODO

- HTTP is mocked with respx; no live Telegram call is made. Live `test-notify` is
  a NIK-TODO once `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` are set.
- `notified_at` is set by the pipeline (Feature 8) only after a successful send;
  unsent matches are retried via a pending-notifications query there.
