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
from urllib.parse import urlsplit

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
from project_pilot.enrichment.fetch import Fetcher, WebFetcher
from project_pilot.enrichment.listing import ListingEnrichmentService
from project_pilot.enrichment.render import PlaywrightFetcher
from project_pilot.enrichment.schemas import ContactEnrichment
from project_pilot.enrichment.search import DuckDuckGoSearch, NullSearchProvider, SearchProvider
from project_pilot.enrichment.service import EnrichmentService
from project_pilot.errors import EnrichmentError, ProjectPilotError
from project_pilot.evaluation.check import CheckService
from project_pilot.evaluation.llm import LlmMatcher, OpenAiStructuredClient, load_prompt
from project_pilot.ingestion.client import BASE_URL, PolitenessClient
from project_pilot.ingestion.normalize import canonicalize_url
from project_pilot.ingestion.parser import ParsedListing, parse_detail_page
from project_pilot.notification.slack import SlackClient, SlackNotifier, SlackWebClient
from project_pilot.notification.slack_bot import SlackBot, run_socket_mode
from project_pilot.pipeline import Pipeline, RunOutcome
from project_pilot.profile_loader import Profile, ProfileService
from project_pilot.reporting import ReportingService, format_report
from project_pilot.scheduler import SchedulerRunner
from project_pilot.selftest import SelfTestReport, SelfTestService, format_selftest

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="project-pilot",
    help="Personal freelancermap.de listing pilot.",
    no_args_is_help=True,
    add_completion=False,
)


def _is_slack_file_url(url: str) -> bool:
    """True only for Slack's own file hosts (where the bot token may be sent)."""
    host = urlsplit(url).hostname or ""
    return host == "slack.com" or host.endswith(".slack.com")


type BotRuntime = tuple[SlackBot, AsyncWebClient, str, Callable[[], Awaitable[None]]]


@app.callback()
def main() -> None:
    """Personal freelancermap.de listing pilot."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


def _load_settings() -> Settings:
    """Load settings and apply the configured (validated) LOG_LEVEL to the root logger."""
    settings = load_settings()
    logging.getLogger().setLevel(settings.log_level.upper())
    return settings


def _slack_client(settings: Settings) -> SlackClient:
    config = settings.require_slack()
    web = cast("SlackWebClient", AsyncWebClient(token=config.bot_token))
    return SlackClient(channel=config.channel, web_client=web)


def _matcher(client: OpenAiStructuredClient, model: str, profile: Profile) -> LlmMatcher:
    """The stage-3 matcher, wired with the profile's no-go technologies."""
    return LlmMatcher(
        client,
        model=model,
        prompt_template=load_prompt(),
        nogo_terms=profile.constraints.nogo_technologies,
    )


def _enrichment_service(
    settings: Settings, profile: Profile
) -> tuple[EnrichmentService, Callable[[], Awaitable[None]]]:
    """Build the enrichment service plus a closer for the fetcher(s) it owns.

    Page fetching honors ``ENRICHMENT_RENDER`` (headless Chromium for JS-rendered
    sites, else httpx). The DuckDuckGo result page is static, so search always uses a
    lightweight httpx fetcher — the browser is reserved for company pages.
    """
    ua = settings.user_agent()
    closeables: list[Fetcher] = []

    if settings.enrichment_render:
        page_fetcher: Fetcher = PlaywrightFetcher(
            user_agent=ua, executable_path=settings.enrichment_render_browser_path or None
        )
        search_fetcher: Fetcher = WebFetcher(user_agent=ua)
        closeables.extend((page_fetcher, search_fetcher))
    else:
        page_fetcher = WebFetcher(user_agent=ua)
        search_fetcher = page_fetcher
        closeables.append(page_fetcher)

    provider: SearchProvider = (
        DuckDuckGoSearch(search_fetcher)
        if settings.enrichment_search == "duckduckgo"
        else NullSearchProvider()
    )
    # The sender's own name comes from profile.md (Contact & Signature), the same
    # source the application signature uses — no separate ENV needed.
    service = EnrichmentService(
        fetcher=page_fetcher,
        search=provider,
        max_pages=settings.enrichment_max_pages,
        sender=profile.applicant_name(),
        offer_du=settings.outreach_offer_du,
    )

    async def closer() -> None:
        for fetcher in closeables:
            await fetcher.aclose()

    return service, closer


def _build_pipeline(settings: Settings) -> tuple[Pipeline, Callable[[], Awaitable[None]]]:
    profile = ProfileService(Path("profile")).load()
    api_key, model = settings.require_openai()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    def client_factory() -> PolitenessClient:
        return PolitenessClient(user_agent=settings.user_agent())

    llm_client = OpenAiStructuredClient(api_key)
    matcher = _matcher(llm_client, model, profile)

    notifier = SlackNotifier(_slack_client(settings)) if settings.has_slack() else None

    pipeline = Pipeline(
        settings=settings,
        profile=profile,
        session_factory=session_factory,
        client_factory=client_factory,
        matcher=matcher,
        notifier=notifier,
        llm_probe=llm_client,
    )

    async def closer() -> None:
        await engine.dispose()

    return pipeline, closer


def _build_bot(settings: Settings) -> BotRuntime:
    """Wire the Slack bot: draft generator, mailer, application service, fetcher, checker."""
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
    checker = CheckService(
        session_factory=session_factory,
        matcher=_matcher(OpenAiStructuredClient(api_key), model, profile),
        profile=profile,
        threshold=settings.match_threshold,
    )
    enrichment: ListingEnrichmentService | None = None
    enrichment_closer: Callable[[], Awaitable[None]] | None = None
    if settings.has_enrichment():
        enrichment_service, enrichment_closer = _enrichment_service(settings, profile)
        enrichment = ListingEnrichmentService(
            session_factory=session_factory, service=enrichment_service
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
        # The bot token is a bearer credential; only ever attach it to Slack's own
        # file hosts. A crafted file event could otherwise point the download URL at
        # an attacker host and capture the xoxb- token from the Authorization header.
        if not _is_slack_file_url(url):
            raise ProjectPilotError(f"refusing to send the Slack token to non-Slack host: {url}")
        headers = {"Authorization": f"Bearer {config.bot_token}"}
        # httpx drops the Authorization header on cross-host redirects, so a redirect
        # to Slack's file CDN keeps working without leaking the token off-Slack.
        async with httpx.AsyncClient(timeout=30.0) as http:
            response = await http.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
            return response.content

    if not config.allowed_user_ids:
        logger.warning(
            "SLACK_ALLOWED_USER_IDS is unset: the bot trusts every member of %s. "
            "Set it to your Slack user id so only you can trigger applications.",
            config.channel,
        )

    slack_bot = SlackBot(
        client=client,
        channel=config.channel,
        service=service,
        fetcher=fetch_listing,
        file_reader=read_slack_file,
        checker=checker,
        enrichment=enrichment,
        allowed_user_ids=config.allowed_user_ids,
    )

    async def closer() -> None:
        if enrichment_closer is not None:
            await enrichment_closer()
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
        # Preflight before any work: a wrong LLM_MODEL, a rotated key or an empty
        # account is announced within seconds of the deploy instead of staying hidden
        # behind runs that silently score every listing as llm_error.
        await pipeline.check_llm()
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


async def _run_selftest(
    settings: Settings, *, text: str | None, listing_id: int | None
) -> SelfTestReport:
    """Wire the real checker and Slack client, then push one listing through both."""
    profile = ProfileService(Path("profile")).load()
    api_key, model = settings.require_openai()
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    client = _slack_client(settings)
    service = SelfTestService(
        checker=CheckService(
            session_factory=session_factory,
            matcher=_matcher(OpenAiStructuredClient(api_key), model, profile),
            profile=profile,
            threshold=settings.match_threshold,
        ),
        poster=client,
        notifier=SlackNotifier(client),
        profile_hash=profile.profile_hash,
    )
    try:
        return await service.run(text=text, listing_id=listing_id)
    finally:
        await engine.dispose()


async def _send_test_notification(settings: Settings) -> bool:
    posted = await _slack_client(settings).post_text(
        "project-pilot test ✅ — if you can see this, Slack is connected."
    )
    return posted is not None


async def _run_enrich(
    settings: Settings,
    *,
    company: str | None,
    listing_id: int | None,
    person: str | None,
    url: str | None,
) -> ContactEnrichment:
    service, closer = _enrichment_service(settings, ProfileService(Path("profile")).load())
    engine = None
    try:
        if listing_id is not None:
            engine = create_engine(settings.database_url)
            session_factory = create_session_factory(engine)
            listing_service = ListingEnrichmentService(
                session_factory=session_factory, service=service
            )
            return await listing_service.enrich_listing(listing_id)
        if not company:
            raise EnrichmentError("provide a company name or --listing-id")
        return await service.enrich(company=company, person=person, known_url=url)
    finally:
        await closer()
        if engine is not None:
            await engine.dispose()


def _format_enrichment(result: ContactEnrichment) -> str:
    lines = [f"Company: {result.company or '—'}", f"Contact: {result.person or '—'}"]
    if result.website:
        lines.append(f"Website: {result.website}")
    lines.append("E-mails: " + (", ".join(result.emails) if result.emails else "none found"))
    lines.append("Phones:  " + (", ".join(result.phones) if result.phones else "none found"))
    if result.persons:
        lines.append("Named on site: " + ", ".join(result.persons))
    lines += ["", "LinkedIn connection message (copy):", f"  {result.linkedin_message}"]
    links = result.links
    lines += [
        "",
        "Research links (open in your browser — nothing is scraped):",
        f"  LinkedIn company: {links.linkedin_company}",
        f"  LinkedIn people:  {links.linkedin_people}",
        f"  Google contact:   {links.google_contact}",
    ]
    if result.sources:
        lines.append("Sources read: " + ", ".join(result.sources))
    return "\n".join(lines)


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
    settings = _load_settings()
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
    settings = _load_settings()
    asyncio.run(_run_daemon(settings))


@app.command("bot")
def bot() -> None:
    """Run only the Slack bot (Apply buttons, /apply and /check commands, thread review)."""
    settings = _load_settings()
    settings.require_slack()
    asyncio.run(_run_bot(settings))


@app.command("healthcheck")
def healthcheck() -> None:
    """Exit 0 if the last successful run is recent (for container healthchecks)."""
    settings = _load_settings()
    if not asyncio.run(_is_healthy(settings)):
        raise typer.Exit(code=1)
    typer.echo("healthy")


@app.command("stats")
def stats() -> None:
    """Print a reporting summary (verdicts, matches per day, no-match terms, tokens)."""
    settings = _load_settings()
    typer.echo(asyncio.run(_build_report(settings)))


@app.command("enrich")
def enrich(
    company: str = typer.Argument(
        None, help="Company name to research (omit when using --listing-id)."
    ),
    listing_id: int = typer.Option(
        None, "--listing-id", "-l", help="Enrich a stored listing's company and record the lead."
    ),
    person: str = typer.Option(None, "--person", "-p", help="Known contact person (First Last)."),
    url: str = typer.Option(None, "--url", "-u", help="Known company website (skips search)."),
) -> None:
    """Find a company's contact data (Impressum/website) plus LinkedIn/Google links."""
    settings = _load_settings()
    if not settings.has_enrichment():
        # The opt-in contract: no outbound search/fetch calls unless enabled.
        typer.echo("enrich is disabled: set ENRICHMENT_ENABLED=true to allow web lookups")
        raise typer.Exit(code=1)
    try:
        result = asyncio.run(
            _run_enrich(settings, company=company, listing_id=listing_id, person=person, url=url)
        )
    except EnrichmentError as err:
        typer.echo(f"enrich failed: {err}")
        raise typer.Exit(code=1) from err
    typer.echo(_format_enrichment(result))


@app.command("test-match")
def test_match(
    text: str | None = typer.Option(
        None, "--text", "-t", help="Description to evaluate (default: the built-in demo listing)."
    ),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="Read the description from a file instead of --text."
    ),
    listing_id: int | None = typer.Option(
        None,
        "--listing-id",
        "-l",
        help="Evaluate a stored listing and post the real match card, so the "
        "Apply and Find-contact buttons work against a running bot.",
    ),
) -> None:
    """Push one listing through hard rules, the LLM, and Slack end to end (nothing is stored)."""
    settings = _load_settings()
    settings.require_slack()
    if file is not None:
        if text is not None:
            typer.echo("use either --text or --file, not both")
            raise typer.Exit(code=1)
        text = file.read_text(encoding="utf-8")
    report = asyncio.run(_run_selftest(settings, text=text, listing_id=listing_id))
    typer.echo(format_selftest(report))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("test-notify")
def test_notify() -> None:
    """Post a test message to the configured Slack channel."""
    settings = _load_settings()
    settings.require_slack()
    sent = asyncio.run(_send_test_notification(settings))
    typer.echo("test notification sent" if sent else "test notification failed")
    if not sent:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
