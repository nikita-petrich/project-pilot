"""Typer command-line interface for project-pilot.

Commands: ``init-db``, ``run-once``, ``daemon``, ``telegram-bot``, ``mcp``,
``healthcheck``, ``stats``, ``enrich``, ``test-match``.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import typer
import uvicorn

from project_pilot.application.cv_drive import CvRefresher, DriveCvRefresher
from project_pilot.application.generator import (
    ApplicationGenerator,
    draft_client,
    load_application_prompt,
)
from project_pilot.application.mailer import SmtpMailer
from project_pilot.application.service import ApplicationService
from project_pilot.config import CvAttachments, Settings, load_settings
from project_pilot.db import create_engine, create_session_factory
from project_pilot.enrichment.fetch import Fetcher, WebFetcher
from project_pilot.enrichment.listing import ListingEnrichmentService
from project_pilot.enrichment.render import PlaywrightFetcher
from project_pilot.enrichment.schemas import ContactDatum, ContactEnrichment
from project_pilot.enrichment.search import DuckDuckGoSearch, NullSearchProvider, SearchProvider
from project_pilot.enrichment.service import EnrichmentService
from project_pilot.errors import EnrichmentError, ProfileUnavailableError
from project_pilot.evaluation.check import CheckService
from project_pilot.evaluation.llm import (
    LlmMatcher,
    MatchLlmClient,
    load_prompt,
    structured_client,
)
from project_pilot.ingestion.client import PolitenessClient
from project_pilot.mcp_server import AsgiApp, McpDeps, build_app
from project_pilot.notification.card import TelegramCardRemover
from project_pilot.notification.telegram import TelegramNotifier
from project_pilot.pipeline import Pipeline, RunOutcome
from project_pilot.profile_loader import Profile, ProfileService
from project_pilot.profile_source import WebProfileSource
from project_pilot.reporting import ReportingService, format_report
from project_pilot.scheduler import SchedulerRunner
from project_pilot.selftest import SelfTestReport, SelfTestService, format_selftest
from project_pilot.telegram_bot import TelegramButtons

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="project-pilot",
    help="Personal freelancermap.de listing pilot.",
    no_args_is_help=True,
    add_completion=False,
)


# httpx logs every request at INFO, URL included — and the Telegram bot token is
# *in* that URL (api.telegram.org/bot<token>/sendMessage). At INFO the secret would
# be in the container log, in `docker compose logs`, and in any pasted snippet. The
# SDKs ship both names: `httpx` and `httpx2` (the Anthropic SDK's fork).
_URL_LOGGING_SILENCED = ("httpx", "httpx2", "httpcore")


@app.callback()
def main() -> None:
    """Personal freelancermap.de listing pilot."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    silence_request_logging()


def silence_request_logging() -> None:
    """Keep request URLs (and the credentials inside them) out of the log."""
    for name in _URL_LOGGING_SILENCED:
        logging.getLogger(name).setLevel(logging.WARNING)


def _load_settings() -> Settings:
    """Load settings and apply the configured (validated) LOG_LEVEL to the root logger."""
    settings = load_settings()
    logging.getLogger().setLevel(settings.log_level.upper())
    # LOG_LEVEL=DEBUG raises the root logger, which would otherwise hand the
    # HTTP loggers their URLs back.
    silence_request_logging()
    return settings


def _matcher(client: MatchLlmClient, model: str, profile: Profile) -> LlmMatcher:
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
    # The sender's own name comes from the profile (Contact & Signature), the same
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


# How long an unattended process waits between attempts while the website is down.
PROFILE_RETRY_SECONDS = 60.0


def _profile_service(settings: Settings) -> ProfileService:
    """The profile assembler: public half from the website, private half from the repo."""
    return ProfileService(
        Path("profile"),
        WebProfileSource(base_url=settings.profile_url, locale=settings.profile_locale),
    )


async def _fetch_profile(settings: Settings) -> Profile:
    """One attempt at the profile, for the commands a human is watching.

    There is no fallback to an older copy by design (see profile_source.py). A
    failure raises and the command exits with the reason on the terminal — the
    person who typed it is already looking there, so it goes nowhere else.
    """
    return await _profile_service(settings).load()


async def _wait_for_profile(
    settings: Settings,
    *,
    retry_seconds: float = PROFILE_RETRY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Profile:
    """The profile for an unattended process: warn once, wait, never restart-loop.

    Exiting on a website outage looked safe and was not: the container restarts,
    fails again, and every boot sent the same Telegram warning — a message every
    few seconds for as long as the site was down. So the daemon and the MCP server
    stay up instead, do no work without a profile, retry every
    ``retry_seconds``, and say two things only: that the profile is gone, once,
    and that it is back, once.
    """
    warned = False
    while True:
        try:
            profile = await _fetch_profile(settings)
        except ProfileUnavailableError as err:
            logger.error("profile unavailable, retrying in %.0fs: %s", retry_seconds, err)
            if not warned:
                await _operator_message(
                    settings,
                    "Profil nicht abrufbar — es wird nichts bewertet und nichts verschickt. "
                    "Neuer Versuch jede Minute; eine Nachricht, sobald es wieder da ist."
                    f"\n\n{err}",
                )
                warned = True
            await sleep(retry_seconds)
            continue
        if warned:
            await _operator_message(
                settings, "✅ Profil wieder erreichbar — es wird wieder bewertet."
            )
        return profile


async def _operator_message(settings: Settings, text: str) -> None:
    """Best-effort operator notice; without Telegram configured the log is the channel."""
    if settings.has_telegram():
        await _notifier(settings).notify_warning(text)


def _load_profile(settings: Settings) -> Profile:
    """The sync entry point, for the one builder that runs before any event loop.

    Only ``_build_mcp_app`` may call this: ``mcp`` builds its app synchronously and
    hands it to uvicorn afterwards, so no loop is running yet. Everything already
    inside ``asyncio.run`` must await instead; ``asyncio.run`` refuses to nest, and
    did, putting the worker into a restart loop on the first deploy.
    """
    return asyncio.run(_wait_for_profile(settings))


async def _build_pipeline(
    settings: Settings, profile: Profile
) -> tuple[Pipeline, Callable[[], Awaitable[None]]]:
    credentials = settings.require_llm()
    model = credentials.model
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    def client_factory() -> PolitenessClient:
        return PolitenessClient(user_agent=settings.user_agent())

    llm_client = structured_client(credentials)
    matcher = _matcher(llm_client, model, profile)

    notifier = _notifier(settings)

    pipeline = Pipeline(
        settings=settings,
        profile=profile,
        session_factory=session_factory,
        client_factory=client_factory,
        matcher=matcher,
        llm_probe=llm_client,
        notifier=notifier,
    )

    async def closer() -> None:
        await engine.dispose()

    return pipeline, closer


def _notifier(settings: Settings) -> TelegramNotifier:
    """The alert channel, with the page every Bewerben button opens."""
    bot_token, chat_id = settings.require_telegram()
    return TelegramNotifier(
        bot_token=bot_token, chat_id=chat_id, session_url=settings.claude_session_url
    )


def _build_cv_refresher(settings: Settings, cvs: CvAttachments) -> CvRefresher | None:
    """Refresh the CVs from the public Drive folder, or ``None`` to use local files as-is."""
    if not settings.cv_drive_folder_id:
        return None
    targets = [path for path in (cvs.de_pdf, cvs.en_pdf) if path is not None]
    if not targets:
        return None
    return DriveCvRefresher(folder_id=settings.cv_drive_folder_id, targets=targets)


def _build_mcp_app(settings: Settings) -> tuple[AsgiApp, Callable[[], Awaitable[None]]]:
    """Wire the MCP server over the same services the pipeline uses."""
    token = settings.require_mcp()
    profile = _load_profile(settings)
    credentials = settings.require_llm()
    model = credentials.model
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    generator = ApplicationGenerator(
        draft_client(credentials), model=model, prompt_template=load_application_prompt()
    )
    mailer = SmtpMailer(settings.require_smtp()) if settings.has_smtp() else None
    cv_attachments = settings.cv_attachments()
    service = ApplicationService(
        session_factory=session_factory,
        generator=generator,
        profile=profile,
        mailer=mailer,
        cv_attachments=cv_attachments,
        cv_refresher=_build_cv_refresher(settings, cv_attachments),
    )
    checker = CheckService(
        session_factory=session_factory,
        matcher=_matcher(structured_client(credentials), model, profile),
        profile=profile,
        threshold=settings.match_threshold,
    )
    # The chat gets the same lookup the CLI gets: the listing-aware one, which
    # knows the named contact, the e-mail already in the ad text and the board's
    # own company page — and records the lead it found.
    enricher, enrichment_closer = _enrichment_service(settings, profile)
    listing_enricher = ListingEnrichmentService(session_factory=session_factory, service=enricher)

    deps = McpDeps(
        session_factory=session_factory,
        check_service=checker,
        application_service=service,
        enricher=listing_enricher,
        # Without Telegram configured there is no card to retire; drafting simply
        # leaves the feed alone rather than failing.
        card_remover=(
            TelegramCardRemover(session_factory=session_factory, notifier=_notifier(settings))
            if settings.has_telegram()
            else None
        ),
    )

    async def closer() -> None:
        await enrichment_closer()
        await engine.dispose()

    return build_app(deps, token=token), closer


async def _run_once(settings: Settings) -> RunOutcome:
    pipeline, closer = await _build_pipeline(settings, await _fetch_profile(settings))
    try:
        return await pipeline.run_once()
    finally:
        await closer()


async def _run_daemon(settings: Settings) -> None:
    pipeline, closer = await _build_pipeline(settings, await _wait_for_profile(settings))
    runner = SchedulerRunner(pipeline.run_once, interval_minutes=settings.scan_interval_min)
    try:
        # Preflight before any work: a wrong LLM_MODEL, a rotated key or an empty
        # account is announced within seconds of the deploy instead of staying hidden
        # behind runs that silently score every listing as llm_error.
        await pipeline.check_llm()
        await pipeline.run_once()  # initial run so the healthcheck has a baseline
        await runner.run_forever()
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
    settings: Settings, *, text: str | None, listing_id: int | None, url: str = ""
) -> SelfTestReport:
    """Wire the real checker and the push channel, then run one listing through both."""
    profile = await _fetch_profile(settings)
    credentials = settings.require_llm()
    model = credentials.model
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    service = SelfTestService(
        checker=CheckService(
            session_factory=session_factory,
            matcher=_matcher(structured_client(credentials), model, profile),
            profile=profile,
            threshold=settings.match_threshold,
        ),
        notifier=_notifier(settings),
        profile_hash=profile.profile_hash,
    )
    try:
        return await service.run(text=text, listing_id=listing_id, url=url)
    finally:
        await engine.dispose()


async def _run_enrich(
    settings: Settings,
    *,
    company: str | None,
    listing_id: int | None,
    person: str | None,
    url: str | None,
) -> ContactEnrichment:
    service, closer = _enrichment_service(settings, await _fetch_profile(settings))
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


def _with_source(data: list[ContactDatum]) -> str:
    """``value (source)`` per entry — the provenance decides how far to trust it."""
    return ", ".join(f"{datum.value} ({datum.source})" for datum in data)


def _format_enrichment(result: ContactEnrichment) -> str:
    lines = [f"Company: {result.company or '—'}", f"Contact: {result.person or '—'}"]
    if result.company_page:
        lines.append(f"Company page: {result.company_page}")
    if result.website:
        lines.append(f"Website: {result.website}")
    lines.append("E-mails: " + (_with_source(result.emails) or "none found"))
    lines.append("Phones:  " + (_with_source(result.phones) or "none found"))
    if result.persons:
        lines.append("Named on site: " + _with_source(result.persons))
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
    try:
        outcome = asyncio.run(_run_once(settings))
    except ProfileUnavailableError as err:
        typer.echo(f"profile unavailable: {err}")
        raise typer.Exit(code=1) from err
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
    settings = _load_settings()
    asyncio.run(_run_daemon(settings))


@app.command("telegram-bot")
def telegram_bot() -> None:
    """Hear the Ablehnen button on the match cards: long polling, no inbound port."""
    settings = _load_settings()
    bot_token, chat_id = settings.require_telegram()
    asyncio.run(TelegramButtons(bot_token=bot_token, chat_id=chat_id).run_forever())


@app.command("mcp")
def mcp() -> None:
    """Serve project-pilot's functions as MCP tools (Streamable HTTP + bearer token)."""
    settings = _load_settings()
    asgi_app, closer = _build_mcp_app(settings)

    async def _serve() -> None:
        config = uvicorn.Config(
            cast("Any", asgi_app), host="0.0.0.0", port=settings.mcp_port, log_level="info"
        )
        try:
            await uvicorn.Server(config).serve()
        finally:
            await closer()

    asyncio.run(_serve())


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
    try:
        result = asyncio.run(
            _run_enrich(settings, company=company, listing_id=listing_id, person=person, url=url)
        )
    except EnrichmentError as err:
        typer.echo(f"enrich failed: {err}")
        raise typer.Exit(code=1) from err
    except ProfileUnavailableError as err:
        typer.echo(f"profile unavailable: {err}")
        raise typer.Exit(code=1) from err
    typer.echo(_format_enrichment(result))


@app.command("test-match")
def test_match(
    text: str | None = typer.Option(
        None, "--text", "-t", help="Description to evaluate instead of a stored listing."
    ),
    file: Path | None = typer.Option(
        None, "--file", "-f", help="Read the description from a file instead of --text."
    ),
    listing_id: int | None = typer.Option(
        None,
        "--listing-id",
        "-l",
        help="Evaluate a specific stored listing instead of the most recent one.",
    ),
    url: str | None = typer.Option(
        None,
        "--url",
        "-u",
        help=(
            "Real listing URL to attach to the card (adds the 'Projektbeschreibung "
            "öffnen' button); only valid with --text/--file, since a stored listing "
            "already has one."
        ),
    ),
) -> None:
    """Push one real listing through hard rules, LLM, and the alert (stores nothing).

    With none of --text/--file/--listing-id given, evaluates the most recently
    stored listing, so the card always carries real project data.
    """
    settings = _load_settings()
    settings.require_telegram()
    if file is not None:
        if text is not None:
            typer.echo("use either --text or --file, not both")
            raise typer.Exit(code=1)
        text = file.read_text(encoding="utf-8")
    if url is not None and listing_id is not None:
        typer.echo("--url only applies to pasted text, not --listing-id (it already has one)")
        raise typer.Exit(code=1)
    if url is not None and text is None:
        typer.echo("--url only applies to --text/--file")
        raise typer.Exit(code=1)
    if text is not None and listing_id is not None:
        typer.echo("use either --text/--file or --listing-id, not both")
        raise typer.Exit(code=1)
    try:
        report = asyncio.run(
            _run_selftest(settings, text=text, listing_id=listing_id, url=url or "")
        )
    except ProfileUnavailableError as err:
        typer.echo(f"profile unavailable: {err}")
        raise typer.Exit(code=1) from err
    typer.echo(format_selftest(report))
    if not report.ok:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
