"""Typer command-line interface for project-pilot.

Commands are wired in as features land: ``init-db``, ``run-once``, ``daemon``,
``test-notify``, ``test-filter`` and ``stats``.
"""

import asyncio

import typer

from project_pilot.config import load_settings
from project_pilot.notification.telegram import TelegramClient

app = typer.Typer(
    name="project-pilot",
    help="Personal freelancermap.de listing pilot.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Personal freelancermap.de listing pilot."""


async def _send_test_notification(bot_token: str, chat_id: str) -> bool:
    async with TelegramClient(bot_token=bot_token, chat_id=chat_id) as client:
        return await client.send_message(
            "<b>project-pilot</b> test notification ✅\nIf you can read this, Telegram is wired up."
        )


@app.command("test-notify")
def test_notify() -> None:
    """Send a test message to the configured Telegram chat."""
    settings = load_settings()
    bot_token, chat_id = settings.require_telegram()
    sent = asyncio.run(_send_test_notification(bot_token, chat_id))
    typer.echo("test notification sent" if sent else "test notification failed")
    if not sent:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
