"""Render the app's ``.env`` from the deploy environment's secrets and variables.

Every secret and variable configured for the workflow becomes one ``KEY=value`` line,
except the handful that configure the deploy itself. Nothing is hardcoded here, so
adding a setting later is a new secret in the ``prod`` environment and never a
workflow change.

The file goes to stdout so the workflow can pipe it straight into ``ssh``: values
never reach the job log. Only counts and key names are ever printed to stderr.
"""

import json
import os
import re
import sys

# These configure the deploy, not the app, so they must not land in the app's .env.
DEPLOY_ONLY = re.compile(r"^(VPS_.*|GITHUB_TOKEN)$", re.IGNORECASE)
VALID_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A bot token as @BotFather hands it out: numeric bot id, colon, secret.
BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_\-]{30,}$")
# A chat id is an integer: positive for the private chat with the bot (the
# intended target), negative for a group or channel.
CHAT_ID_RE = re.compile(r"^-?\d+$")
# The routine's API trigger, as the modal at claude.ai/code/routines shows it.
FIRE_URL_RE = re.compile(
    r"^https://api\.anthropic\.com/v1/claude_code/routines/trig_[A-Za-z0-9]+/fire$"
)
ROUTINE_TOKEN_RE = re.compile(r"^sk-ant-oat01-")

# Without these the container dies at boot (see project_pilot.cli._build_pipeline and
# Pipeline.run_once), so failing here beats debugging a crash loop over SSH.
REQUIRED = (
    "OPENAI_API_KEY",
    "LLM_MODEL",
    "SEARCH_URLS",
    # Telegram is THE alert channel; without it the daemon aborts at boot by
    # design, so the deploy refuses here instead.
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    # The routine whose fire opens one Claude session per match: without it the
    # daemon aborts at boot too, because a card whose Bewerben leads nowhere is
    # the one thing the alert must never be.
    "CLAUDE_ROUTINE_FIRE_URL",
    "CLAUDE_ROUTINE_TOKEN",
    # The MCP service refuses to start without its bearer token.
    "MCP_TOKEN",
    # The reverse proxy's Docker network. Wrong or unset, the MCP container comes
    # up healthy and stays unreachable — a 502 with nothing in its own logs, which
    # is exactly the kind of silent failure this gate exists to prevent.
    "PROXY_NETWORK",
)

# The app starts without these, but not usefully: no contact mail, or a default password.
RECOMMENDED = (
    "CONTACT_MAIL",
    "POSTGRES_PASSWORD",
)


def _load(variable: str) -> dict[str, str]:
    """Parse one ``toJSON()`` blob into plain strings, ignoring anything malformed."""
    parsed: object = json.loads(os.environ.get(variable) or "{}")
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def collect() -> dict[str, str]:
    """Merge variables and secrets into the app's settings; secrets win on a clash."""
    merged: dict[str, str] = {}
    for blob in ("VARS_JSON", "SECRETS_JSON"):
        for key, value in _load(blob).items():
            if not DEPLOY_ONLY.match(key):
                merged[key] = value
    return merged


def problems(settings: dict[str, str]) -> list[str]:
    """Reasons the rendered file would not yield a working container.

    Values are checked for shapes a ``.env`` cannot carry unambiguously. Rejecting
    them loudly beats writing a file that a dotenv reader silently truncates.
    """
    found = [f"{key} is not set" for key in REQUIRED if not settings.get(key)]
    for key, value in sorted(settings.items()):
        if not VALID_NAME.match(key):
            found.append(f"{key!r} is not a usable environment variable name")
        elif "\n" in value or "\r" in value:
            found.append(f"{key} spans several lines, which a .env cannot represent")
        elif value != value.strip():
            found.append(f"{key} has leading or trailing whitespace")
        elif " #" in value:
            found.append(f"{key} contains ' #', which dotenv readers cut off as a comment")
    found.extend(_bot_token_problems(settings.get("TELEGRAM_BOT_TOKEN", "")))
    found.extend(_chat_id_problems(settings.get("TELEGRAM_CHAT_ID", "")))
    found.extend(_routine_problems(settings))
    return found


def _chat_id_problems(chat_id: str) -> list[str]:
    """Catch a value that is not a chat id at all (the bot's @name, a username)."""
    if not chat_id:
        return []  # absence is already reported by the REQUIRED check
    if not CHAT_ID_RE.match(chat_id):
        return [
            f"TELEGRAM_CHAT_ID is {chat_id!r}, which is not a chat id. Expected the "
            "numeric id of your private chat with the bot, as getUpdates reports it."
        ]
    return []


def _routine_problems(settings: dict[str, str]) -> list[str]:
    """Catch the two values of the routine's API trigger swapped or mistyped.

    Both come from one modal at claude.ai/code/routines; the usual mix-ups are
    pasting the routine page's URL instead of the fire endpoint, or an API key
    where the per-routine token belongs. Either answers 4xx at the first match.
    """
    found: list[str] = []
    fire_url = settings.get("CLAUDE_ROUTINE_FIRE_URL", "")
    if fire_url and not FIRE_URL_RE.match(fire_url):
        found.append(
            "CLAUDE_ROUTINE_FIRE_URL does not look like a routine fire endpoint. Expected "
            "https://api.anthropic.com/v1/claude_code/routines/trig_.../fire, as the "
            "API-trigger modal shows it."
        )
    token = settings.get("CLAUDE_ROUTINE_TOKEN", "")
    if token and not ROUTINE_TOKEN_RE.match(token):
        found.append(
            "CLAUDE_ROUTINE_TOKEN does not look like a routine token (sk-ant-oat01-...). "
            "It is the per-routine token from the API-trigger modal, not an API key."
        )
    return found


def _bot_token_problems(token: str) -> list[str]:
    """Catch a value that is not a bot token at all.

    The usual mix-up is pasting the chat id, the bot's @name, or the API URL
    here. All of them answer 401 at the first real match, hours after a deploy
    that looked healthy. The shape is fixed, so check it here — without echoing
    the secret into the log.
    """
    if not token:
        return []  # absence is already reported by the REQUIRED check
    if not BOT_TOKEN_RE.match(token):
        return [
            "TELEGRAM_BOT_TOKEN does not look like a bot token. Expected the "
            "value @BotFather hands out, digits then a colon then the secret "
            "(e.g. 123456789:AA...), not the chat id or the bot's @name."
        ]
    return []


def main() -> int:
    settings = collect()

    found = problems(settings)
    if found:
        for problem in found:
            print(f"::error::{problem}", file=sys.stderr)
        return 1

    for key in RECOMMENDED:
        if not settings.get(key):
            print(f"::warning::{key} is not set in the prod environment", file=sys.stderr)

    body = "".join(f"{key}={settings[key]}\n" for key in sorted(settings))
    sys.stdout.write("# Generated by the deploy workflow — edits here are overwritten.\n")
    sys.stdout.write(body)

    print(f"rendered {len(settings)} settings", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
