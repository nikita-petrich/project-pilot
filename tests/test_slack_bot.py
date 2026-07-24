"""Tests for the Slack bot routing (fake poster + fake application service)."""

from project_pilot.application.service import DraftView
from project_pilot.errors import ApplicationStateError, EmailSendError
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import ApplicationStatus, PostedPrecision, RemoteStatus
from project_pilot.notification.slack import Block, PostedMessage
from project_pilot.notification.slack_bot import USAGE, SlackBot

CHANNEL = "C1"


def _view(
    *,
    application_id: int = 1,
    recipient: str | None = "pm@firma.de",
    status: ApplicationStatus = ApplicationStatus.READY,
) -> DraftView:
    return DraftView(
        application_id=application_id,
        title="KI-Projekt",
        url="https://www.freelancermap.de/projekt/ki-projekt",
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


class _FakePoster:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str | None]] = []
        self.blocks: list[tuple[list[Block], str, str | None]] = []
        self.updates: list[tuple[str, str]] = []
        self._ts = 100

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> PostedMessage | None:
        self._ts += 1
        self.blocks.append((blocks, text, thread_ts))
        return PostedMessage(channel=CHANNEL, ts=str(self._ts))

    async def post_text(self, text: str, *, thread_ts: str | None = None) -> PostedMessage | None:
        self.texts.append((text, thread_ts))
        return PostedMessage(channel=CHANNEL, ts="1")

    async def update_blocks(self, channel: str, ts: str, blocks: list[Block], text: str) -> bool:
        self.updates.append((channel, ts))
        return True

    def all_text(self) -> list[str]:
        return [t for t, _ in self.texts]


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


def _bot(
    poster: _FakePoster, service: _FakeService, *, fetched: ParsedListing | None = None
) -> SlackBot:
    async def fetcher(url: str) -> ParsedListing:
        assert fetched is not None
        return fetched

    return SlackBot(
        client=poster,
        channel=CHANNEL,
        service=service,
        fetcher=fetcher if fetched is not None else None,
    )


def _interactive(
    action_id: str, value: str, *, channel: str = CHANNEL, ts: str = "111.1"
) -> dict[str, object]:
    return {
        "type": "block_actions",
        "channel": {"id": channel},
        "message": {"ts": ts},
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


def _slash(text: str) -> dict[str, object]:
    return {"command": "/apply", "text": text, "channel_id": CHANNEL}


async def test_apply_action_drafts_and_records_ref() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("interactive", _interactive("apply", "7"))
    assert ("draft_for_listing", 7) in service.calls
    assert len(poster.blocks) == 1  # draft posted as one message
    assert service.recorded == [(1, "C1:101")]  # (application_id, channel:ts)


async def test_apply_from_foreign_channel_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("interactive", _interactive("apply", "7", channel="C2"))
    assert service.calls == []
    assert poster.blocks == []


async def test_send_action_updates_and_confirms_with_linkedin() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.view = _view(status=ApplicationStatus.SENT)
    await _bot(poster, service).dispatch("interactive", _interactive("send", "1", ts="222.2"))
    assert ("send", 1) in service.calls
    assert ("C1", "222.2") in poster.updates  # the draft message is updated in place
    confirmation = poster.all_text()[-1]
    assert "verschickt an" in confirmation and "Hallo!" in confirmation


async def test_send_action_surfaces_error_in_thread() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.error = EmailSendError("smtp down")
    await _bot(poster, service).dispatch("interactive", _interactive("send", "1", ts="222.2"))
    text, thread_ts = poster.texts[-1]
    assert "smtp down" in text and thread_ts == "222.2"


async def test_cancel_action_updates_message() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.view = _view(status=ApplicationStatus.CANCELLED)
    await _bot(poster, service).dispatch("interactive", _interactive("cancel", "1", ts="9.9"))
    assert ("cancel", 1) in service.calls
    assert ("C1", "9.9") in poster.updates


async def test_open_mail_action_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).on_block_action("open_mail", None, CHANNEL, "1.1")
    assert service.calls == []


async def test_slash_apply_text_drafts_from_text() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("slash_commands", _slash("Python Projekt gesucht"))
    assert ("draft_from_text", "Python Projekt gesucht") in service.calls


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
    assert "Projektbeschreibung" in poster.all_text()[-1]


async def test_slash_apply_empty_shows_usage() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).dispatch("slash_commands", _slash("   "))
    assert service.calls == []
    assert poster.all_text()[-1] == USAGE


async def test_thread_reply_with_email_sets_recipient() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    await _bot(poster, service).dispatch("events_api", _event("neu@firma.de"))
    assert ("set_recipient", (1, "neu@firma.de")) in service.calls
    assert ("C1", "111.1") in poster.updates


async def test_thread_reply_with_instruction_revises() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    await _bot(poster, service).dispatch("events_api", _event("Bitte kürzer"))
    assert ("revise", (1, "Bitte kürzer")) in service.calls


async def test_thread_reply_error_posts_in_thread() -> None:
    poster, service = _FakePoster(), _FakeService()
    service.by_ref["C1:111.1"] = _view()
    service.error = ApplicationStateError("nicht erlaubt")
    await _bot(poster, service).dispatch("events_api", _event("Bitte kürzer"))
    text, thread_ts = poster.texts[-1]
    assert "nicht erlaubt" in text and thread_ts == "111.1"


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
