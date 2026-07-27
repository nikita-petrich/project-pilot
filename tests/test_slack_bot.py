"""Tests for the Slack bot routing (fake poster + fake application service)."""

from collections.abc import Awaitable, Callable

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


def _block_text(block: Block) -> str:
    text = block.get("text")
    return str(text.get("text")) if isinstance(text, dict) else ""


def _is_draft(blocks: list[Block]) -> bool:
    return any(b.get("type") == "header" and "Application draft" in _block_text(b) for b in blocks)


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


def _bot(
    poster: _FakePoster,
    service: _FakeService,
    *,
    fetched: ParsedListing | None = None,
    file_reader: Callable[[str], Awaitable[bytes]] | None = None,
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
    )


def _file_event(
    name: str, *, channel: str = CHANNEL, url: str | None = "https://files.slack/x"
) -> dict[str, object]:
    file: dict[str, object] = {"name": name}
    if url is not None:
        file["url_private_download"] = url
    return {"event": {"type": "message", "channel": channel, "files": [file]}}


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


def _slash(text: str) -> dict[str, object]:
    return {"command": "/apply", "text": text, "channel_id": CHANNEL}


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
    service.view = _view(status=ApplicationStatus.SENT)
    await _bot(poster, service).dispatch(
        "interactive", _interactive("send", "1", ts="222.2", thread_ts="111.1")
    )
    assert ("send", 1) in service.calls
    assert "222.2" in poster.draft_update_ids()  # the draft message is updated in place
    assert any("sent to" in t and "Hallo!" in t for t in poster.visible_texts())


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


async def test_open_mail_action_is_ignored() -> None:
    poster, service = _FakePoster(), _FakeService()
    await _bot(poster, service).on_block_action("open_mail", None, CHANNEL, "1.1")
    assert service.calls == []


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
