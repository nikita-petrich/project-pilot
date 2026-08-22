"""MCP server: tool payloads, tool registration, and the bearer guard."""

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.application.service import ApplicationService, DraftView
from project_pilot.errors import ApplicationStateError
from project_pilot.evaluation.check import CheckResult, CheckService
from project_pilot.mcp_server import (
    McpDeps,
    Receive,
    Scope,
    Send,
    _check_payload,
    _draft_payload,
    _listing_summary,
    bearer_guard,
    build_mcp,
    enrich_company,
    get_listing,
    list_matches,
)
from project_pilot.models import (
    ApplicationStatus,
    Evaluation,
    EvaluationStage,
    Listing,
    ListingStatus,
    RemoteStatus,
    Verdict,
)

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)

EXPECTED_TOOLS = {
    "project_pilot_list_matches",
    "project_pilot_get_listing",
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


def _deps(session_factory: async_sessionmaker[AsyncSession]) -> McpDeps:
    # The DB tools never touch check/application/enrichment, so opaque stand-ins
    # are enough — constructing the real services would drag in OpenAI clients.
    return McpDeps(
        session_factory=session_factory,
        check_service=None,  # type: ignore[arg-type]
        application_service=None,  # type: ignore[arg-type]
        enricher=None,
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


async def test_build_mcp_registers_all_tools() -> None:
    deps = McpDeps(
        session_factory=None,  # type: ignore[arg-type]
        check_service=CheckService.__new__(CheckService),
        application_service=ApplicationService.__new__(ApplicationService),
        enricher=None,
    )
    tools = {tool.name for tool in await build_mcp(deps).list_tools()}
    assert tools == EXPECTED_TOOLS


async def test_enrich_company_disabled_raises(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ApplicationStateError, match="ENRICHMENT_ENABLED"):
        await enrich_company(_deps(session_factory), listing_id=1)


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


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_bearer_guard_rejects_missing_and_wrong_token() -> None:
    app = bearer_guard(_ok_app, "s3cret")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mcp") as client:
        assert (await client.get("/mcp")).status_code == 401
        wrong = await client.get("/mcp", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        ok = await client.get("/mcp", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert ok.text == "ok"
