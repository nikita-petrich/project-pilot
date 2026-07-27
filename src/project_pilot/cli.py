"""Typer command-line interface for project-pilot.

Commands: ``init-db``, ``run-once``, ``daemon``, ``bot``, ``healthcheck``,
``stats``, ``test-notify``.
"""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import httpx
import typer
from slack_sdk.web.async_client import AsyncWebClient

from project_pilot.application.generator import (
    ApplicationGenerator,
    OpenAiDraftClient,
    load_application_prompt,
)
from project_pilot.application.mailer import SmtpMailer
from project_pilot.application.service import ApplicationService
from project_pilot.config import SOURCE_NAME, Settings, load_settings
from project_pilot.db import create_engine, create_session_factory
from project_pilot.evaluation.llm import LlmMatcher, OpenAiStructuredClient, load_prompt
from project_pilot.ingestion.client import BASE_URL, PolitenessClient
from project_pilot.ingestion.normalize import canonicalize_url
from project_pilot.ingestion.parser import ParsedListing, parse_detail_page
from project_pilot.notification.slack import SlackClient, SlackNotifier, SlackWebClient
from project_pilot.notification.slack_bot import SlackBot, run_socket_mode
from project_pilot.pipeline import Pipeline, RunOutcome
from project_pilot.profile_loader import ProfileService
from project_pilot.reporting import ReportingService, format_report
from project_pilot.scheduler import SchedulerRunner

app = typer.Typer(
    name="project-pilot",
    help="Personal freelancermap.de listing pilot.",
    no_args_is_help=True,
    add_completion=False,
)

type BotRuntime = tuple[SlackBot, AsyncWebClient, str, Callable[[], Awaitable[None]]]


@app.callback()
def main() -> None:
    """Personal freelancermap.de listing pilot."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def _slack_client(settings: Settings) -> SlackClient:
    config = settings.require_slack()
    web = cast("SlackWebClient", AsyncWebClient(token=config.bot_token))
    return SlackClient(channel=config.channel, web_client=web)


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

    notifier = SlackNotifier(_slack_client(settings)) if settings.has_slack() else None

    pipeline = Pipeline(
        settings=settings,
        profile=profile,
        session_factory=session_factory,
        client_factory=client_factory,
        matcher=matcher,
        notifier=notifier,
    )

    async def closer() -> None:
        await engine.dispose()

    return pipeline, closer


def _build_bot(settings: Settings) -> BotRuntime:
    """Wire the Slack bot: draft generator, mailer, application service, fetcher."""
    profile = ProfileService(Path("profile")).load()
    api_key, model = settings.require_openai()
    config = settings.require_slack()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    generator = ApplicationGenerator(
        OpenAiDraftClient(api_key), model=model, prompt_template=load_application_prompt()
    )
    mailer = SmtpMailer(settings.require_smtp()) if settings.has_smtp() else None
    service = ApplicationService(
        session_factory=session_factory,
        generator=generator,
        profile=profile,
        mailer=mailer,
        cv_attachments=settings.cv_attachments(),
    )
    web = AsyncWebClient(token=config.bot_token)
    client = SlackClient(channel=config.channel, web_client=cast("SlackWebClient", web))

    async def fetch_listing(url: str) -> ParsedListing:
        politeness = PolitenessClient(user_agent=settings.user_agent())
        try:
            await politeness.check_robots([url])
            response = await politeness.get(url)
            return parse_detail_page(
                response.text,
                BASE_URL,
                source=SOURCE_NAME,
                external_url=canonicalize_url(url, BASE_URL),
            )
        finally:
            await politeness.aclose()

    async def read_slack_file(url: str) -> bytes:
        headers = {"Authorization": f"Bearer {config.bot_token}"}
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.content

    slack_bot = SlackBot(
        client=client,
        channel=config.channel,
        service=service,
        fetcher=fetch_listing,
        file_reader=read_slack_file,
    )

    async def closer() -> None:
        await engine.dispose()

    return slack_bot, web, config.app_token, closer


async def _run_once(settings: Settings) -> RunOutcome:
    pipeline, closer = _build_pipeline(settings)
    try:
        return await pipeline.run_once()
    finally:
        await closer()


async def _run_daemon(settings: Settings) -> None:
    pipeline, closer = _build_pipeline(settings)
    runner = SchedulerRunner(pipeline.run_once, interval_minutes=settings.scan_interval_min)
    bot_runtime: BotRuntime | None = _build_bot(settings) if settings.has_slack() else None
    try:
        await pipeline.run_once()  # initial run so the healthcheck has a baseline
        if bot_runtime is None:
            await runner.run_forever()
        else:
            bot, web, app_token, _ = bot_runtime
            await asyncio.gather(
                runner.run_forever(),
                run_socket_mode(
                    bot=bot, app_token=app_token, web_client=web, stop=runner.stop_event
                ),
            )
    finally:
        if bot_runtime is not None:
            await bot_runtime[3]()
        await closer()


async def _run_bot(settings: Settings) -> None:
    bot, web, app_token, closer = _build_bot(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_socket_mode(bot=bot, app_token=app_token, web_client=web, stop=stop)
    finally:
        await closer()


async def _is_healthy(settings: Settings) -> bool:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            return await ReportingService(session).is_healthy(
                interval_minutes=settings.scan_interval_min
            )
    finally:
        await engine.dispose()


async def _build_report(settings: Settings) -> str:
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            report = await ReportingService(session).build_report()
        return format_report(report)
    finally:
        await engine.dispose()


async def _send_test_notification(settings: Settings) -> bool:
    posted = await _slack_client(settings).post_text(
        "project-pilot test ✅ — if you can see this, Slack is connected."
    )
    return posted is not None


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
    """Run the scheduler (scan every SCAN_INTERVAL_MIN minutes) plus the Slack bot until SIGTERM."""
    settings = load_settings()
    asyncio.run(_run_daemon(settings))


@app.command("bot")
def bot() -> None:
    """Run only the Slack bot (Apply buttons, /apply command, thread review)."""
    settings = load_settings()
    settings.require_slack()
    asyncio.run(_run_bot(settings))


@app.command("healthcheck")
def healthcheck() -> None:
    """Exit 0 if the last successful run is recent (for container healthchecks)."""
    settings = load_settings()
    if not asyncio.run(_is_healthy(settings)):
        raise typer.Exit(code=1)
    typer.echo("healthy")


@app.command("stats")
def stats() -> None:
    """Print a reporting summary (verdicts, matches per day, no-match terms, tokens)."""
    settings = load_settings()
    typer.echo(asyncio.run(_build_report(settings)))


@app.command("test-notify")
def test_notify() -> None:
    """Post a test message to the configured Slack channel."""
    settings = load_settings()
    settings.require_slack()
    sent = asyncio.run(_send_test_notification(settings))
    typer.echo("test notification sent" if sent else "test notification failed")
    if not sent:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
