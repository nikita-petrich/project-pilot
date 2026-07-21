"""Typer command-line interface for project-pilot.

Commands: ``init-db``, ``run-once``, ``daemon``, ``test-notify`` (``test-filter``
and ``stats`` land with their features).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import typer

from project_pilot.config import Settings, load_settings
from project_pilot.db import create_engine, create_session_factory
from project_pilot.evaluation.llm import LlmMatcher, OpenAiStructuredClient, load_prompt
from project_pilot.ingestion.client import PolitenessClient
from project_pilot.notification.telegram import TelegramClient
from project_pilot.pipeline import Pipeline, RunOutcome
from project_pilot.profile_loader import ProfileService
from project_pilot.scheduler import SchedulerRunner

app = typer.Typer(
    name="project-pilot",
    help="Personal freelancermap.de listing pilot.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Personal freelancermap.de listing pilot."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def _build_pipeline(settings: Settings) -> tuple[Pipeline, Callable[[], Awaitable[None]]]:
    profile = ProfileService(Path("profile")).load()
    api_key, model = settings.require_openai()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    def client_factory() -> PolitenessClient:
        return PolitenessClient(user_agent=settings.user_agent())

    matcher = LlmMatcher(
        OpenAiStructuredClient(api_key), model=model, prompt_template=load_prompt()
    )

    telegram: TelegramClient | None = None
    if settings.telegram_bot_token and settings.telegram_chat_id:
        telegram = TelegramClient(
            bot_token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id
        )

    pipeline = Pipeline(
        settings=settings,
        profile=profile,
        session_factory=session_factory,
        client_factory=client_factory,
        matcher=matcher,
        telegram=telegram,
    )

    async def closer() -> None:
        if telegram is not None:
            await telegram.aclose()
        await engine.dispose()

    return pipeline, closer


async def _run_once(settings: Settings) -> RunOutcome:
    pipeline, closer = _build_pipeline(settings)
    try:
        return await pipeline.run_once()
    finally:
        await closer()


async def _run_daemon(settings: Settings) -> None:
    pipeline, closer = _build_pipeline(settings)
    runner = SchedulerRunner(pipeline.run_once, interval_minutes=settings.scan_interval_min)
    try:
        await runner.run_forever()
    finally:
        await closer()


async def _send_test_notification(bot_token: str, chat_id: str) -> bool:
    async with TelegramClient(bot_token=bot_token, chat_id=chat_id) as client:
        return await client.send_message(
            "<b>project-pilot</b> test notification ✅\nIf you can read this, Telegram is wired up."
        )


@app.command("init-db")
def init_db() -> None:
    """Apply database migrations (alembic upgrade head)."""
    from alembic import command
    from alembic.config import Config

    command.upgrade(Config("alembic.ini"), "head")
    typer.echo("database schema is at head")


@app.command("run-once")
def run_once() -> None:
    """Run a single scan. Cron-friendly: non-zero exit on a failed run."""
    settings = load_settings()
    outcome = asyncio.run(_run_once(settings))
    typer.echo(
        f"run {outcome.status.value}: fetched={outcome.fetched} new={outcome.new} "
        f"evaluated={outcome.evaluated} matched={outcome.matched} "
        f"notified={outcome.notified} errors={outcome.errors}"
    )
    if outcome.is_error:
        raise typer.Exit(code=1)


@app.command("daemon")
def daemon() -> None:
    """Run the scheduler (scan every SCAN_INTERVAL_MIN minutes) until SIGTERM."""
    settings = load_settings()
    asyncio.run(_run_daemon(settings))


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
