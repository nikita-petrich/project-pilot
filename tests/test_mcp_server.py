"""MCP server: tool payloads, tool registration, and the token guard."""

from dataclasses import asdict, replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.application.service import ApplicationService, DraftView
from project_pilot.enrichment.links import build_links
from project_pilot.enrichment.schemas import ContactDatum, ContactEnrichment
from project_pilot.errors import ApplicationStateError, EnrichmentError
from project_pilot.evaluation.check import CheckResult, CheckService
from project_pilot.ingestion.client import BASE_URL
from project_pilot.ingestion.normalize import canonicalize_url, compute_url_hash
from project_pilot.mcp_server import (
    McpDeps,
    Receive,
    Scope,
    Send,
    _check_payload,
    _draft_payload,
    _enrichment_payload,
    _listing_summary,
    build_mcp,
    draft_application,
    enrich_company,
    get_listing,
    ingest_listing,
    list_matches,
    match_card,
    token_guard,
)
from project_pilot.models import (
    ApplicationStatus,
    Evaluation,
    EvaluationStage,
    Listing,
    ListingOrigin,
    ListingStatus,
    RemoteStatus,
    Verdict,
)
from project_pilot.repository import Repository

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

EXPECTED_TOOLS = {
    "project_pilot_list_matches",
    "project_pilot_get_listing",
    "project_pilot_match_card",
    "project_pilot_ingest_listing",
    "project_pilot_check_listing",
    "project_pilot_check_text",
    "project_pilot_draft_application",
    "project_pilot_revise_application",
    "project_pilot_set_recipient",
    "project_pilot_send_application",
    "project_pilot_enrich_company",
}


def _listing(url: str = "https://example.com/p/1", score: int = 80) -> Listing:
    listing = Listing(
        source="freelancermap",
        external_url=url,
        url_hash=url,
        title="Senior Python Developer",
        description="LLM-Pipelines mit FastAPI.",
        status=ListingStatus.EVALUATED,
        remote_status=RemoteStatus.REMOTE,
        origin=ListingOrigin.SCAN,
        first_seen_at=NOW,
        last_seen_at=NOW,
        raw={"company": "ACME GmbH"},
    )
    listing.evaluations.append(
        Evaluation(
            stage=EvaluationStage.LLM,
            verdict=Verdict.MATCH,
            score=score,
            reason={"reasons": ["passt"]},
            created_at=NOW,
        )
    )
    return listing


class _Threshold:
    """All match_card wants from the check service: the score's yardstick."""

    threshold = 60


class _NoEnricher:
    """A stand-in for the tools that never enrich; calling it is the test failing."""

    async def enrich_listing(self, listing_id: int) -> ContactEnrichment:
        raise AssertionError("this tool must not run an enrichment lookup")


def _deps(session_factory: async_sessionmaker[AsyncSession]) -> McpDeps:
    # The DB tools never run a check, draft or enrichment, so opaque stand-ins
    # are enough — constructing the real services would drag in OpenAI clients.
    return McpDeps(
        session_factory=session_factory,
        check_service=_Threshold(),  # type: ignore[arg-type]
        application_service=None,  # type: ignore[arg-type]
        enricher=_NoEnricher(),
    )


def test_listing_summary_uses_best_llm_score() -> None:
    listing = _listing()
    listing.evaluations.append(
        Evaluation(stage=EvaluationStage.LLM, verdict=Verdict.MATCH, score=91, created_at=NOW)
    )
    summary = _listing_summary(listing)
    assert summary["score"] == 91
    assert summary["remote_status"] == "remote"


def test_check_payload_mirrors_matchverdict_fields() -> None:
    result = CheckResult(
        title="T",
        stage=EvaluationStage.LLM,
        verdict=Verdict.NO_MATCH,
        passed=False,
        score=10,
        threshold=60,
        reason={"reasons": ["zu wenig KI"]},
        message=None,
        is_llm_error=False,
    )
    payload = _check_payload(result)
    assert payload["verdict"] == "no_match"
    assert payload["passed"] is False
    assert "reasons" not in payload  # only present when a message was built


def test_the_draft_payload_says_where_to_paste_the_linkedin_note() -> None:
    # A note without the profile it belongs to means looking the person up by
    # hand every time, which is exactly the step worth removing.
    view = DraftView(
        application_id=7,
        listing_id=1,
        title="AI Developer",
        url="https://example.com/p/1",
        company="Weissenberg Business Consulting GmbH",
        contact_name="Sebastian Koch",
        recipient=None,
        subject="s",
        body="b",
        linkedin_message="l",
        status=ApplicationStatus.AWAITING_EMAIL,
        revision_count=0,
        attachments=(),
        missing_attachments=(),
    )
    search = _draft_payload(view)["linkedin_search"]
    assert isinstance(search, str)
    assert search.startswith("https://www.linkedin.com/search/results/people/")
    assert "Sebastian+Koch" in search
    assert "Weissenberg" in search


def test_draft_payload_carries_send_status() -> None:
    view = DraftView(
        application_id=1,
        title="T",
        url=None,
        contact_name=None,
        recipient="jobs@acme.de",
        subject="Bewerbung",
        body="Sehr geehrte…",
        linkedin_message="Hi",
        status=ApplicationStatus.READY,
        revision_count=0,
    )
    payload = _draft_payload(view)
    assert payload["status"] == "ready"
    assert payload["recipient"] == "jobs@acme.de"


def _bare_deps() -> McpDeps:
    """Deps with nothing wired: enough to inspect what build_mcp registers."""
    return McpDeps(
        session_factory=None,  # type: ignore[arg-type]
        check_service=CheckService.__new__(CheckService),
        application_service=ApplicationService.__new__(ApplicationService),
        enricher=_NoEnricher(),
    )


async def test_build_mcp_registers_all_tools() -> None:
    tools = {tool.name for tool in await build_mcp(_bare_deps()).list_tools()}
    assert tools == EXPECTED_TOOLS


class _MissingListing:
    async def enrich_listing(self, listing_id: int) -> ContactEnrichment:
        raise EnrichmentError(f"Listing {listing_id} not found")


async def test_enriching_a_listing_that_is_gone_answers_in_the_tool_protocol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The lookup's own error would reach the chat as an unhandled exception; the
    # chat needs a sentence it can read out instead.
    deps = replace(_deps(session_factory), enricher=_MissingListing())
    with pytest.raises(ApplicationStateError, match="not found"):
        await enrich_company(deps, listing_id=999)


async def test_list_matches_returns_feed_newest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        older = _listing("https://example.com/p/1", score=70)
        newer = _listing("https://example.com/p/2", score=90)
        newer.first_seen_at = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
        session.add_all([older, newer])
        await session.commit()

    feed = await list_matches(_deps(session_factory), limit=5)
    assert [entry["score"] for entry in feed] == [90, 70]


async def test_get_listing_includes_description_and_evaluations(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        listing = _listing()
        session.add(listing)
        await session.commit()
        listing_id = listing.id

    detail = await get_listing(_deps(session_factory), listing_id)
    description = detail["description"]
    assert isinstance(description, str) and description.startswith("LLM-Pipelines")
    evaluations = detail["evaluations"]
    assert isinstance(evaluations, list) and evaluations[0]["verdict"] == "match"

    with pytest.raises(ApplicationStateError):
        await get_listing(_deps(session_factory), listing_id + 999)


async def test_match_card_hands_back_the_alert_s_own_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One layout for three surfaces: whoever prints this needs no layout of its
    # own, so the skill and the Telegram alert cannot drift into two cards.
    async with session_factory() as session:
        listing = _listing()
        session.add(listing)
        await session.commit()
        listing_id = listing.id

    deps = _deps(session_factory)
    payload = await match_card(deps, listing_id)
    card = payload["card"]
    assert isinstance(card, str)
    assert card.startswith("⭐ 80 · Senior Python Developer · ACME GmbH")
    assert "🏢 Company: ACME GmbH" in card
    assert f"🗄 DB: listing_id {listing_id}" in card
    assert "📏 Schwelle: 60 (erreicht)" in card  # the yardstick comes from CheckService
    assert "👥 LinkedIn Firma:" in card

    with pytest.raises(ApplicationStateError):
        await match_card(deps, listing_id + 999)


async def test_get_listing_returns_the_facts_that_only_live_in_raw(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Company and contact person are parsed out of the source record, not stored
    # as columns — without them a chat reading the listing back would have to
    # guess a salutation it could have known.
    async with session_factory() as session:
        listing = _listing()
        listing.raw = {
            "company": "ACME GmbH",
            "firstName": "Max",
            "lastName": "Mustermann",
            "isEndcustomerProject": True,
            "workload": 100,
            "durationText": "6 Monate",
            "expires": "2026-09-30T00:00:00",
            "companyUrl": "/firma/556-acme-gmbh",
        }
        session.add(listing)
        await session.commit()
        listing_id = listing.id

    detail = await get_listing(_deps(session_factory), listing_id)
    assert detail["company"] == "ACME GmbH"
    assert detail["contact_name"] == "Max Mustermann"
    assert detail["client_type"] == "direct"
    assert detail["workload"] == "100%"
    assert detail["duration"] == "6 Monate"
    assert detail["apply_by"] == "30.09.2026"
    # The board's own company page, resolved against the listing's URL rather than
    # assembled from the id and the company name — a rename must not silently
    # produce a dead link.
    assert detail["company_page"] == "https://example.com/firma/556-acme-gmbh"


async def test_get_listing_leaves_an_unstated_client_type_unknown(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A source that says nothing is not the same as a source that says "agency".
    async with session_factory() as session:
        listing = _listing()
        session.add(listing)
        await session.commit()
        listing_id = listing.id

    assert (await get_listing(_deps(session_factory), listing_id))["client_type"] is None


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_token_guard_rejects_missing_and_wrong_token() -> None:
    app = token_guard(_ok_app, "s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        assert (await client.get("/mcp")).status_code == 401
        wrong = await client.get("/mcp", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = await client.get("/mcp", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert ok.text == "ok"


async def test_token_guard_accepts_the_token_as_a_url_prefix() -> None:
    """The connector route: a client that can only be given a URL, no headers."""
    seen: list[str] = []

    async def _echo_path(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(str(scope["path"]))
        await _ok_app(scope, receive, send)

    app = token_guard(_echo_path, "s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        assert (await client.get("/t/s3cret/mcp")).status_code == 200
        assert (await client.get("/t/s3cret")).status_code == 200
        assert (await client.get("/t/nope/mcp")).status_code == 401
        assert (await client.get("/t/")).status_code == 401

    # The wrapped app sees the path it would have seen with an Authorization header.
    assert seen == ["/mcp", "/"]


async def test_ingest_listing_stores_provenance_and_dedupes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deps = _deps(session_factory)
    text = "Position: Senior Python Developer\n\nRAG-Pipelines mit FastAPI, remote."

    first = await ingest_listing(deps, text, "mail", note="Recruiter-Mail von ACME")
    assert first["already_known"] is False
    assert first["origin"] == "mail"
    assert first["title"] == "Senior Python Developer"
    listing_id = first["listing_id"]

    # The same mail pasted again is the same listing, not a second row.
    again = await ingest_listing(deps, f"  {text}  ", "chat")
    assert again["already_known"] is True
    assert again["listing_id"] == listing_id

    async with session_factory() as session:
        stored = await Repository(session).get_listing_with_evaluations(int(str(listing_id)))
    assert stored is not None
    assert stored.origin is ListingOrigin.MAIL
    ingest = stored.raw["ingest"]
    assert isinstance(ingest, dict)
    assert ingest["origin"] == "mail" and ingest["note"] == "Recruiter-Mail von ACME"


async def test_ingest_listing_with_a_url_shares_the_scanner_key(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pasted link and the scanned page must be one row, not two."""
    url = "https://www.freelancermap.de/projekt/beispiel-12345"
    async with session_factory() as session:
        scanned = _listing(url=url)
        scanned.url_hash = compute_url_hash(canonicalize_url(url, BASE_URL))
        scanned.external_url = canonicalize_url(url, BASE_URL)
        session.add(scanned)
        await session.commit()
        scanned_id = scanned.id

    result = await ingest_listing(
        _deps(session_factory), "Irgendein anderer Text.", "url", url=f"{url}?utm_source=x"
    )
    assert result["already_known"] is True
    assert result["listing_id"] == scanned_id


async def test_ingest_listing_rejects_empty_text_and_bad_origin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    deps = _deps(session_factory)
    with pytest.raises(ApplicationStateError, match="empty"):
        await ingest_listing(deps, "   ", "chat")
    with pytest.raises(ApplicationStateError, match="Unknown origin"):
        await ingest_listing(deps, "Text", "telepathy")
    # `scan` is the scanner's own label; an ingest must name the real channel.
    with pytest.raises(ApplicationStateError, match="scanner"):
        await ingest_listing(deps, "Text", "scan")


async def test_prompts_are_discoverable_with_their_descriptions() -> None:
    """The command menu of every surface is generated from this list."""
    prompts = await build_mcp(_bare_deps()).list_prompts()
    assert {prompt.name for prompt in prompts} == {
        "check_project",
        "write_application",
        "send_application",
        "enrich_company",
    }
    # The description is what a bot renders next to the command, so it must be there.
    assert all(prompt.description for prompt in prompts)


async def test_prompt_body_carries_the_argument_and_names_its_tools() -> None:
    mcp = build_mcp(_bare_deps())
    rendered = await mcp.render_prompt("check_project", {"argument": "Listing 42"})
    text = str(rendered.messages[0].content)
    assert "Listing 42" in text
    # The procedure must route through the tools, not around them.
    assert "project_pilot_check_listing" in text
    assert "project_pilot_ingest_listing" in text


async def test_send_prompt_demands_an_explicit_confirmation() -> None:
    # The one irreversible action: the wording that gates it is worth a test.
    mcp = build_mcp(_bare_deps())
    rendered = await mcp.render_prompt("send_application", {"argument": "12"})
    text = str(rendered.messages[0].content)
    assert "explicit yes" in text
    assert "project_pilot_send_application" in text


async def test_missing_argument_does_not_leave_a_raw_placeholder() -> None:
    mcp = build_mcp(_bare_deps())
    rendered = await mcp.render_prompt("write_application", {})
    text = str(rendered.messages[0].content)
    assert "{listing}" not in text
    assert "(not given)" in text


class _FakeDrafts:
    """Only the one call draft_application makes."""

    def __init__(self, view: DraftView) -> None:
        self._view = view

    async def draft_for_listing(self, listing_id: int) -> DraftView:
        return self._view


class _Remover:
    def __init__(self, *, fails: bool = False) -> None:
        self.removed: list[int] = []
        self._fails = fails

    async def remove_card(self, listing_id: int) -> bool:
        self.removed.append(listing_id)
        if self._fails:
            raise RuntimeError("telegram unreachable")
        return True


def _draft_deps(remover: _Remover | None) -> McpDeps:
    view = DraftView(
        application_id=7,
        listing_id=447,
        title="AI Developer",
        url=None,
        contact_name=None,
        recipient=None,
        subject="s",
        body="b",
        linkedin_message="l",
        status=ApplicationStatus.AWAITING_EMAIL,
        revision_count=0,
    )
    return McpDeps(
        session_factory=None,  # type: ignore[arg-type]
        check_service=CheckService.__new__(CheckService),
        application_service=_FakeDrafts(view),  # type: ignore[arg-type]
        enricher=_NoEnricher(),
        card_remover=remover,
    )


async def test_drafting_retires_the_match_card() -> None:
    # Telegram reports no press on a URL button, so the Bewerben tap itself is
    # invisible; the draft it leads to is the first thing we can actually see.
    remover = _Remover()

    payload = await draft_application(_draft_deps(remover), listing_id=447)

    assert remover.removed == [447]
    assert payload["application_id"] == 7


async def test_a_card_that_will_not_go_away_never_costs_the_draft() -> None:
    # The feed's tidiness is cosmetic; the draft is the work.
    payload = await draft_application(_draft_deps(_Remover(fails=True)), listing_id=447)
    assert payload["application_id"] == 7


async def test_drafting_without_telegram_configured_just_drafts() -> None:
    payload = await draft_application(_draft_deps(None), listing_id=447)
    assert payload["application_id"] == 7


def test_the_enrichment_payload_says_where_the_connection_note_goes() -> None:
    # The note and the profile it is pasted on belong together; shipping the note
    # alone leaves the lookup as a manual step every single time.
    result = ContactEnrichment(
        company="Weissenberg Business Consulting GmbH",
        person="Sebastian Koch",
        website="https://weissenberg-solutions.de/",
        links=build_links(company="Weissenberg Business Consulting GmbH", person="Sebastian Koch"),
        linkedin_message="Guten Tag Sebastian Koch, …",
        emails=[ContactDatum("i.strucken@weissenberg.de", "freelancermap")],
    )

    payload = _enrichment_payload(result)

    search = payload["linkedin_search"]
    assert isinstance(search, str)
    assert search.startswith("https://www.linkedin.com/search/results/people/")
    assert "Sebastian+Koch" in search
    assert payload["links"] == asdict(result.links)
    assert payload["emails"] == [{"value": "i.strucken@weissenberg.de", "source": "freelancermap"}]
