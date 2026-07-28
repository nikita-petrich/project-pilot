"""Tests for the Slack bot routing (fake poster + fake application service)."""

from collections.abc import Awaitable, Callable

from project_pilot.application.service import DraftView
from project_pilot.enrichment.schemas import ContactEnrichment, DiscoveryLinks
from project_pilot.errors import ApplicationStateError, EmailSendError, EnrichmentError
from project_pilot.evaluation.check import CheckResult
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import (
    ApplicationStatus,
    EvaluationStage,
    PostedPrecision,
    RemoteStatus,
    Verdict,
)
from project_pilot.notification.messages import MatchMessage
from project_pilot.notification.slack import Block, PostedMessage
from project_pilot.notification.slack_bot import CHECK_USAGE, USAGE, EnrichmentFlow, SlackBot

CHANNEL = "C1"


def _view(
    *,
    application_id: int = 1,
    recipient: str | None = "pm@firma.de",
    status: ApplicationStatus = ApplicationStatus.READY,
    contact_name: str | None = None,
) -> DraftView:
    return DraftView(
        application_id=application_id,
        title="KI-Projekt",
        url="https://www.freelancermap.de/projekt/ki-projekt",
        contact_name=contact_name,
        recipient=recipient,
        subject="Bewerbung: KI-Projekt",
        body="Sehr geehrte Damen und Herren",
        linkedin_message="Hallo!",
        status=status,
        revision_count=0,
    )


def _parsed() -> ParsedListing:
    return ParsedListing(
        source="freelancermap",
        external_url="https://www.freelancermap.de/projekt/neu",
        url_hash="h" * 64,
        title="Neu",
        description="desc",
        skills=[],
        start_date=None,
        start_asap=False,
        end_date=None,
        location=None,
        remote_status=RemoteStatus.UNKNOWN,
        posted_at=None,
        posted_at_precision=PostedPrecision.UNKNOWN,
        raw={},
    )


def _block_text(block: Block) -> str:
    text = block.get("text")
    return str(text.get("text")) if isinstance(text, dict) else ""


def _is_draft(blocks: list[Block]) -> bool:
    return any(b.get("type") == "header" and "Application draft" in _block_text(b) for b in blocks)


def _button_elements(blocks: list[Block]) -> list[dict[str, object]]:
    elements: list[dict[str, object]] = []
    for block in blocks:
        if block.get("type") != "actions":
            continue
        block_elements = block.get("elements")
        assert isinstance(block_elements, list)
        elements.extend(element for element in block_elements if isinstance(element, dict))
    return elements


class _FakePoster:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str | None]] = []
        self.posted_blocks: list[tuple[list[Block], str | None]] = []
        self.updates: list[tuple[str, str, list[Block]]] = []
        self._ts = 100

    async def post_text(self, text: str, *, thread_ts: str | None = None) -> PostedMessage | None:
        self._ts += 1
        self.texts.append((text, thread_ts))
        return PostedMessage(channel=CHANNEL, ts=str(self._ts))

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> PostedMessage | None:
        self._ts += 1
        self.posted_blocks.append((blocks, thread_ts))
        return PostedMessage(channel=CHANNEL, ts=str(self._ts))

    async def update_blocks(self, channel: str, ts: str, blocks: list[Block], text: str) -> bool:
        self.updates.append((channel, ts, blocks))
        return True

    def visible_texts(self) -> list[str]:
        """Everything the user can read: plain posts plus status-block updates."""
        out = [t for t, _ in self.texts]
        for _, _, blocks in self.updates:
            out.extend(_block_text(b) for b in blocks if b.get("type") == "section")
        return out

    def draft_rendered(self) -> bool:
        return any(_is_draft(blocks) for _, _, blocks in self.updates) or any(
            _is_draft(blocks) for blocks, _ in self.posted_blocks
        )

    def draft_update_ids(self) -> list[str]:
        return [ts for _, ts, blocks in self.updates if _is_draft(blocks)]


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.view = _view()
        self.recorded: list[tuple[int, str]] = []
        self.by_ref: dict[str, DraftView] = {}
        self.known_urls: dict[str, int] = {}
        self.error: Exception | None = None

    def _result(self, name: str, arg: object) -> DraftView:
        self.calls.append((name, arg))
        if self.error is not None:
            raise self.error
        return self.view

    async def draft_for_listing(self, listing_id: int) -> DraftView:
        return self._result("draft_for_listing", listing_id)

    async def draft_from_parsed(self, parsed: ParsedListing) -> DraftView:
        return self._result("draft_from_parsed", parsed.external_url)

    async def draft_from_text(self, text: str) -> DraftView:
        return self._result("draft_from_text", text)

    async def revise(self, application_id: int, instruction: str) -> DraftView:
        return self._result("revise", (application_id, instruction))

    async def set_recipient(self, application_id: int, email: str) -> DraftView:
        return self._result("set_recipient", (application_id, email))

    async def send(self, application_id: int) -> DraftView:
        return self._result("send", application_id)

    async def cancel(self, application_id: int) -> DraftView:
        return self._result("cancel", application_id)

    async def record_draft_ref(self, application_id: int, draft_ref: str) -> None:
        self.recorded.append((application_id, draft_ref))

    async def find_by_draft_ref(self, draft_ref: str) -> DraftView | None:
        return self.by_ref.get(draft_ref)

    async def find_listing_id_by_url(self, url: str) -> int | None:
        return self.known_urls.get(url)


def _check_result(*, passed: bool = True, with_message: bool = True) -> CheckResult:
    return CheckResult(
        title="KI-Projekt",
        stage=EvaluationStage.LLM,
        verdict=Verdict.MATCH if passed else Verdict.NO_MATCH,
        passed=passed,
        score=80 if passed else 20,
        threshold=60,
        reason={"reasons": ["passt"]},
        message=MatchMessage(title="KI-Projekt", url="", score=80)
        if passed and with_message
        else None,
        is_llm_error=False,
    )


class _FakeChecker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.result = _check_result()

    def _record(self, name: str, arg: object) -> CheckResult:
        self.calls.append((name, arg))
        return self.result

    async def check_stored(self, listing_id: int) -> CheckResult:
        return self._record("check_stored", listing_id)

    async def check_parsed(self, parsed: ParsedListing) -> CheckResult:
        return self._record("check_parsed", parsed.external_url)

    async def check_text(self, text: str) -> CheckResult:
        return self._record("check_text", text)


class _FakeEnrichment:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.error: Exception | None = None

    async def enrich_listing(self, listing_id: int) -> ContactEnrichment:
        self.calls.append(listing_id)
        if self.error is not None:
            raise self.error
        return ContactEnrichment(
            company="Muster GmbH",
            person="Max Mustermann",
            website="https://muster-gmbh.de/",
            links=DiscoveryLinks(
                linkedin_company="https://www.linkedin.com/search/results/companies/?keywords=Muster",
                linkedin_people="https://www.linkedin.com/search/results/people/?keywords=Max",
                google_company="https://www.google.com/search?q=Muster",
                google_contact="https://www.google.com/search?q=Muster+Impressum",
            ),
            emails=["bewerbung@muster-gmbh.de"],
            phones=["+49 30 1234567"],
            persons=["Max Mustermann"],
            sources=["https://muster-gmbh.de/impressum"],
        )


def _bot(
    poster: _FakePoster,
    service: _FakeService,
    *,
    fetched: ParsedListing | None = None,
    file_reader: Callable[[str], Awaitable[bytes]] | None = None,
    checker: _FakeChecker | None = None,
    enrichment: EnrichmentFlow | None = None,
) -> SlackBot:
    async def fetcher(url: str) -> ParsedListing:
        assert fetched is not None
        return fetched

    return SlackBot(
        client=poster,
        channel=CHANNEL,
        service=service,
        fetcher=fetcher if fetched is not None else None,
        file_reader=file_reader,
        checker=checker,
        enrichment=enrichment,
    )


def _file_event(
    name: str,
    *,
    channel: str = CHANNEL,
    url: str | None = "https://files.slack/x",
    text: str = "",
) -> dict[str, object]:
    file: dict[str, object] = {"name": name}
    if url is not None:
        file["url_private_download"] = url
    return {"event": {"type": "message", "channel": channel, "text": text, "files": [file]}}


def _interactive(
    action_id: str,
    value: str | None,
    *,
    channel: str = CHANNEL,
    ts: str = "111.1",
    thread_ts: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {"ts": ts}
    if thread_ts is not None:
        message["thread_ts"] = thread_ts
    return {
        "type": "block_actions",
        "channel": {"id": channel},
        "message": message,
        "actions": [{"action_id": action_id, "value": value}],
    }


def _event(
    text: str, *, channel: str = CHANNEL, thread_ts: str | None = "111.1", bot: bool = False
) -> dict[str, object]:
    event: dict[str, object] = {"type": "message", "channel": channel, "text": text}
    if thread_ts is not None:
        event["thread_ts"] = thread_ts
    if bot:
        event["bot_id"] = "B1"
    return {"event": event}


def _slash(text: str, command: str = "/apply") -> dict[str, object]:
    return {"command": command, "text": text, "channel_id": CHANNEL}


def _buttons(blocks: list[Block]) -> dict[str, str | None]:
    """Map ``action_id -> value`` for every button in the given blocks."""
    out: dict[str, str | None] = {}
    for block in blocks:
        if block.get("type") != "actions":
            continue
        elements = block.get("elements")
        assert isinstance(elements, list)
        for element in elements:
            if isinstance(element, dict):
                value = element.get("value")
                out[str(element.get("action_id"))] = str(value) if value is not None else None
    return out


def _updated_buttons(poster: _FakePoster) -> dict[str, str | None]:
    assert poster.updates, "no message update happened"
    return _buttons(poster.updates[-1][2])


async def test_apply_action_threads_draft_and_records_root_ref() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("interactive", _interactive("apply", "7"))
    assert ("draft_for_listing", 7) in service.calls
    # progress placeholder is posted in the match's thread, then filled with the draft
    assert any("⏳" in text and thread == "111.1" for text, thread in poster.texts)
    assert poster.draft_rendered()
    # routing key is the thread root (the match message ts), not the draft's own ts
    assert service.recorded == [(1, "C1:111.1")]


async def test_apply_from_foreign_channel_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("interactive", _interactive("apply", "7", channel="C2"))
    assert service.calls == []
    assert poster.texts == [] and poster.updates == []


async def test_send_action_updates_draft_and_confirms_with_linkedin() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.view = _view(status=ApplicationStatus.SENT, contact_name="Anna Kleinen")
    await _bot(poster, service).dispatch(
        "interactive", _interactive("send", "1", ts="222.2", thread_ts="111.1")
    )
    assert ("send", 1) in service.calls
    assert "222.2" in poster.draft_update_ids()  # the draft message is updated in place
    visible = "\n".join(poster.visible_texts())
    assert "sent to" in visible and "Hallo!" in visible
    # the confirmation carries the LinkedIn people-search button for the contact
    buttons = [e for _, _, blocks in poster.updates for e in _button_elements(blocks)]
    assert any(
        e.get("action_id") == "linkedin_search" and "Anna%20Kleinen" in str(e.get("url"))
        for e in buttons
    )


async def test_send_action_surfaces_error() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.error = EmailSendError("smtp down")
    await _bot(poster, service).dispatch("interactive", _interactive("send", "1", ts="222.2"))
    assert any("smtp down" in t for t in poster.visible_texts())


async def test_cancel_action_updates_message() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.view = _view(status=ApplicationStatus.CANCELLED)
    await _bot(poster, service).dispatch("interactive", _interactive("cancel", "1", ts="9.9"))
    assert ("cancel", 1) in service.calls
    assert "9.9" in [ts for _, ts, _ in poster.updates]


async def test_url_button_action_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).on_block_action("open_project", None, CHANNEL, "1.1")
    assert service.calls == []


def _contact_rendered(poster: _FakePoster) -> bool:
    def is_contact(blocks: list[Block]) -> bool:
        return any(
            b.get("type") == "header" and "Contact research" in _block_text(b) for b in blocks
        )

    return any(is_contact(blocks) for _, _, blocks in poster.updates) or any(
        is_contact(blocks) for blocks, _ in poster.posted_blocks
    )


async def test_enrich_action_posts_contact_blocks() -> None:
    poster, service, enrichment = _FakePoster(), _FakeService(), _FakeEnrichment()
    await _bot(poster, service, enrichment=enrichment).dispatch(
        "interactive", _interactive("enrich", "42", thread_ts="111.1")
    )
    assert enrichment.calls == [42]
    assert _contact_rendered(poster)
    rendered = [_block_text(b) for _, _, blocks in poster.updates for b in blocks]
    assert any("bewerbung@muster-gmbh.de" in text for text in rendered)


async def test_enrich_action_without_service_hints_to_enable() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("interactive", _interactive("enrich", "42"))
    assert any("ENRICHMENT_ENABLED" in t for t in poster.visible_texts())


async def test_enrich_action_surfaces_error() -> None:
    poster, service, enrichment = _FakePoster(), _FakeService(), _FakeEnrichment()
    enrichment.error = EnrichmentError("Listing 42 not found")
    await _bot(poster, service, enrichment=enrichment).dispatch(
        "interactive", _interactive("enrich", "42")
    )
    assert any("not found" in t for t in poster.visible_texts())


async def test_slash_apply_text_drafts_from_text_in_thread() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("slash_commands", _slash("Python Projekt gesucht"))
    assert ("draft_from_text", "Python Projekt gesucht") in service.calls
    assert poster.draft_rendered()
    # a parent message is created and the draft lives in its thread
    assert service.recorded and service.recorded[0][0] == 1


async def test_slash_apply_known_url_uses_stored_listing() -> None:
    poster, service = _FakePoster(), _FakeService()
    url = "https://www.freelancermap.de/projekt/ki-projekt"
    service.known_urls[url] = 7
    await _bot(poster, service).dispatch("slash_commands", _slash(url))
    assert ("draft_for_listing", 7) in service.calls


async def test_slash_apply_unknown_freelancermap_url_fetches() -> None:
    poster, service = _FakePoster(), _FakeService()
    parsed = _parsed()
    await _bot(poster, service, fetched=parsed).dispatch(
        "slash_commands", _slash("https://www.freelancermap.de/projekt/neu")
    )
    assert ("draft_from_parsed", parsed.external_url) in service.calls


async def test_slash_apply_foreign_url_hints() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("slash_commands", _slash("https://example.com/job"))
    assert service.calls == []
    assert any("project description" in t for t in poster.visible_texts())


async def test_slash_apply_empty_shows_usage() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("slash_commands", _slash("   "))
    assert service.calls == []
    assert poster.texts[-1][0] == USAGE


async def test_thread_reply_with_email_sets_recipient() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    await _bot(poster, service).dispatch("events_api", _event("neu@firma.de"))
    assert ("set_recipient", (1, "neu@firma.de")) in service.calls
    assert poster.draft_rendered()


async def test_thread_reply_with_slack_autolinked_email_sets_recipient() -> None:
    # Slack rewrites a bare address into <mailto:a@b|a@b>; it must still set the recipient.
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    await _bot(poster, service).dispatch("events_api", _event("<mailto:neu@firma.de|neu@firma.de>"))
    assert ("set_recipient", (1, "neu@firma.de")) in service.calls


async def test_thread_reply_with_instruction_revises() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    await _bot(poster, service).dispatch("events_api", _event("Bitte kürzer"))
    assert ("revise", (1, "Bitte kürzer")) in service.calls
    assert any("⏳" in t or "Revising" in t for t in poster.visible_texts())


async def test_thread_reply_error_is_shown() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    service.error = ApplicationStateError("nicht erlaubt")
    await _bot(poster, service).dispatch("events_api", _event("Bitte kürzer"))
    assert any("nicht erlaubt" in t for t in poster.visible_texts())


async def test_bot_message_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    await _bot(poster, service).dispatch("events_api", _event("egal", bot=True))
    assert service.calls == []


async def test_top_level_message_without_thread_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("events_api", _event("hi", thread_ts=None))
    assert service.calls == []


async def test_reply_to_unknown_thread_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("events_api", _event("Bitte kürzer"))
    assert service.calls == []
    assert poster.texts == []


async def test_file_upload_drafts_from_extracted_text() -> None:
    poster, service = _FakePoster(), _FakeService()

    async def reader(url: str) -> bytes:
        assert url == "https://files.slack/x"
        return b"Senior Python Backend Engineer gesucht"

    await _bot(poster, service, file_reader=reader).dispatch(
        "events_api", _file_event("projekt.txt")
    )
    assert any(
        name == "draft_from_text" and "Python Backend" in str(arg) for name, arg in service.calls
    )
    assert poster.draft_rendered()


async def test_file_upload_without_reader_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("events_api", _file_event("x.txt"))
    assert service.calls == [] and poster.texts == []


async def test_file_upload_from_foreign_channel_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()

    async def reader(url: str) -> bytes:
        return b"text"

    await _bot(poster, service, file_reader=reader).dispatch(
        "events_api", _file_event("x.txt", channel="C2")
    )
    assert service.calls == []


async def test_bot_file_upload_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()

    async def reader(url: str) -> bytes:
        return b"text"

    event = _file_event("x.txt")
    envelope = event["event"]
    assert isinstance(envelope, dict)
    envelope["bot_id"] = "B1"  # the bot's own uploads must not loop back
    await _bot(poster, service, file_reader=reader).dispatch("events_api", event)
    assert service.calls == []


async def test_slash_check_text_match_renders_apply_button_for_remembered_text() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()
    bot = _bot(poster, service, checker=checker)
    await bot.dispatch("slash_commands", _slash("Python RAG Projekt", command="/check"))
    assert ("check_text", "Python RAG Projekt") in checker.calls
    buttons = _updated_buttons(poster)
    key = buttons["apply_check"]
    assert key is not None  # the placeholder ts routes back to the checked text
    # clicking the button drafts from exactly the checked text
    await bot.dispatch("interactive", _interactive("apply_check", key))
    assert ("draft_from_text", "Python RAG Projekt") in service.calls
    assert poster.draft_rendered()


async def test_slash_check_no_match_renders_verdict_without_apply_button() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()
    checker.result = _check_result(passed=False)
    await _bot(poster, service, checker=checker).dispatch(
        "slash_commands", _slash("Java Projekt", command="/check")
    )
    assert not _updated_buttons(poster)
    joined = "\n".join(poster.visible_texts())
    assert "20/100" in joined


async def test_slash_check_known_url_checks_stored_listing() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()
    url = "https://www.freelancermap.de/projekt/ki-projekt"
    service.known_urls[url] = 7
    await _bot(poster, service, checker=checker).dispatch(
        "slash_commands", _slash(url, command="/check")
    )
    assert ("check_stored", 7) in checker.calls
    assert _updated_buttons(poster)["apply"] == "7"  # the normal stored-listing apply flow


async def test_slash_check_unknown_freelancermap_url_fetches_and_offers_apply_url() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()
    parsed = _parsed()
    url = "https://www.freelancermap.de/projekt/neu"
    await _bot(poster, service, fetched=parsed, checker=checker).dispatch(
        "slash_commands", _slash(url, command="/check")
    )
    assert ("check_parsed", parsed.external_url) in checker.calls
    assert _updated_buttons(poster)["apply_url"] == url


async def test_apply_url_action_drafts_via_fetch() -> None:
    poster, service = _FakePoster(), _FakeService()
    parsed = _parsed()
    await _bot(poster, service, fetched=parsed).dispatch(
        "interactive", _interactive("apply_url", "https://www.freelancermap.de/projekt/neu")
    )
    assert ("draft_from_parsed", parsed.external_url) in service.calls
    assert poster.draft_rendered()


async def test_apply_check_action_with_unknown_key_hints_expiry() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service, checker=_FakeChecker()).dispatch(
        "interactive", _interactive("apply_check", "123.456")
    )
    assert service.calls == []
    assert any("expired" in t for t in poster.visible_texts())


async def test_slash_check_foreign_url_hints() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()
    await _bot(poster, service, checker=checker).dispatch(
        "slash_commands", _slash("https://example.com/job", command="/check")
    )
    assert checker.calls == []
    assert any("project description" in t for t in poster.visible_texts())


async def test_slash_check_empty_shows_usage() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service, checker=_FakeChecker()).dispatch(
        "slash_commands", _slash("  ", command="/check")
    )
    assert poster.texts[-1][0] == CHECK_USAGE


async def test_slash_check_error_is_surfaced() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()
    url = "https://www.freelancermap.de/projekt/ki-projekt"
    service.known_urls[url] = 7

    async def boom(listing_id: int) -> CheckResult:
        raise ApplicationStateError("Project 7 not found")

    checker.check_stored = boom  # type: ignore[method-assign]
    await _bot(poster, service, checker=checker).dispatch(
        "slash_commands", _slash(url, command="/check")
    )
    assert any("Project 7 not found" in t for t in poster.visible_texts())


async def test_file_upload_with_check_comment_checks_extracted_text() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()

    async def reader(url: str) -> bytes:
        return b"Senior Python Backend Engineer gesucht"

    await _bot(poster, service, file_reader=reader, checker=checker).dispatch(
        "events_api", _file_event("projekt.txt", text="check das bitte")
    )
    assert any(name == "check_text" and "Python Backend" in str(arg) for name, arg in checker.calls)
    assert service.calls == []  # no draft — checking only
    key = _updated_buttons(poster)["apply_check"]
    assert key is not None  # a passing file check still offers the apply button


async def test_file_upload_without_check_comment_still_drafts() -> None:
    poster, service, checker = _FakePoster(), _FakeService(), _FakeChecker()

    async def reader(url: str) -> bytes:
        return b"Projektbeschreibung"

    await _bot(poster, service, file_reader=reader, checker=checker).dispatch(
        "events_api", _file_event("projekt.txt", text="bitte bewerben")
    )
    assert checker.calls == []
    assert any(name == "draft_from_text" for name, _ in service.calls)
