"""Tests for the Telegram bot routing (fake client and fake application service)."""

import asyncio

from project_pilot.application.service import DraftView
from project_pilot.errors import ApplicationStateError, EmailSendError
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.models import ApplicationStatus, PostedPrecision, RemoteStatus
from project_pilot.notification.bot import HELP_TEXT, TelegramBot
from project_pilot.notification.telegram import TelegramUpdate

CHAT_ID = "42"


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


class _FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, object] | None]] = []
        self.answered: list[tuple[str, str | None]] = []
        self._next_message_id = 100

    async def get_updates(
        self, *, offset: int | None = None, timeout_s: int = 25
    ) -> list[TelegramUpdate] | None:
        return []

    async def send(
        self,
        text: str,
        *,
        disable_preview: bool = True,
        reply_markup: dict[str, object] | None = None,
    ) -> int | None:
        self.sent.append((text, reply_markup))
        self._next_message_id += 1
        return self._next_message_id

    async def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        self.answered.append((callback_query_id, text))
        return True

    def texts(self) -> list[str]:
        return [text for text, _ in self.sent]


class _FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.view = _view()
        self.recorded: list[tuple[int, int]] = []
        self.by_message: dict[int, DraftView] = {}
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

    async def record_draft_message(self, application_id: int, message_id: int) -> None:
        self.recorded.append((application_id, message_id))

    async def find_by_draft_message(self, message_id: int) -> DraftView | None:
        return self.by_message.get(message_id)

    async def find_listing_id_by_url(self, url: str) -> int | None:
        return self.known_urls.get(url)


def _bot(
    telegram: _FakeTelegram,
    service: _FakeService,
    *,
    fetched: ParsedListing | None = None,
) -> TelegramBot:
    async def fetcher(url: str) -> ParsedListing:
        assert fetched is not None
        return fetched

    return TelegramBot(
        client=telegram,
        chat_id=CHAT_ID,
        service=service,
        fetcher=fetcher if fetched is not None else None,
    )


def _callback(data: str, *, chat_id: str = CHAT_ID, query_id: str = "cb1") -> TelegramUpdate:
    return TelegramUpdate.model_validate(
        {
            "update_id": 1,
            "callback_query": {
                "id": query_id,
                "data": data,
                "message": {"message_id": 10, "chat": {"id": int(chat_id)}},
            },
        }
    )


def _message(text: str, *, chat_id: str = CHAT_ID, reply_to: int | None = None) -> TelegramUpdate:
    message: dict[str, object] = {
        "message_id": 11,
        "text": text,
        "chat": {"id": int(chat_id)},
    }
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "chat": {"id": int(chat_id)}}
    return TelegramUpdate.model_validate({"update_id": 1, "message": message})


def _keyboard_actions(markup: dict[str, object] | None) -> list[str]:
    assert isinstance(markup, dict)
    rows = markup["inline_keyboard"]
    assert isinstance(rows, list)
    actions: list[str] = []
    for row in rows:
        assert isinstance(row, list)
        for button in row:
            assert isinstance(button, dict)
            actions.append(str(button["callback_data"]))
    return actions


async def test_apply_callback_creates_and_shows_draft() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_callback("apply:7"))
    assert ("draft_for_listing", 7) in service.calls
    assert telegram.answered[0][1] is not None  # progress feedback on the tap
    text, markup = telegram.sent[-1]
    assert "Bewerbungsentwurf" in text
    assert "pm@firma.de" in text
    assert _keyboard_actions(markup) == ["send:1", "cancel:1"]
    assert service.recorded == [(1, 101)]


async def test_draft_without_recipient_asks_for_email_and_hides_send() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.view = _view(recipient=None, status=ApplicationStatus.AWAITING_EMAIL)
    await _bot(telegram, service).process_update(_callback("apply:7"))
    text, markup = telegram.sent[-1]
    assert "E-Mail-Adresse" in text
    assert _keyboard_actions(markup) == ["cancel:1"]


async def test_callback_from_foreign_chat_is_ignored() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_callback("apply:7", chat_id="666"))
    assert service.calls == []
    assert telegram.sent == []
    assert len(telegram.answered) == 1  # still acked so the spinner stops


async def test_send_callback_confirms_with_linkedin_message() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.view = _view(status=ApplicationStatus.SENT)
    await _bot(telegram, service).process_update(_callback("send:1"))
    assert ("send", 1) in service.calls
    confirmation = telegram.texts()[-1]
    assert "pm@firma.de" in confirmation
    assert "Hallo!" in confirmation


async def test_send_callback_surfaces_state_errors() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.error = ApplicationStateError("Diese Bewerbung wurde bereits verschickt")
    await _bot(telegram, service).process_update(_callback("send:1"))
    assert "bereits verschickt" in telegram.texts()[-1]


async def test_send_callback_surfaces_smtp_errors() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.error = EmailSendError("smtp send to x failed")
    await _bot(telegram, service).process_update(_callback("send:1"))
    assert "smtp send" in telegram.texts()[-1]


async def test_cancel_callback_confirms() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.view = _view(status=ApplicationStatus.CANCELLED)
    await _bot(telegram, service).process_update(_callback("cancel:1"))
    assert ("cancel", 1) in service.calls
    assert "verworfen" in telegram.texts()[-1]


async def test_apply_command_with_text_drafts_from_text() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_message("/apply Python Projekt gesucht"))
    assert ("draft_from_text", "Python Projekt gesucht") in service.calls


async def test_apply_command_with_known_url_uses_stored_listing() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    url = "https://www.freelancermap.de/projekt/ki-projekt"
    service.known_urls[url] = 7
    await _bot(telegram, service).process_update(_message(f"/apply {url}"))
    assert ("draft_for_listing", 7) in service.calls


async def test_apply_command_with_unknown_freelancermap_url_fetches() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    parsed = _parsed()
    await _bot(telegram, service, fetched=parsed).process_update(
        _message("/apply https://www.freelancermap.de/projekt/neu")
    )
    assert ("draft_from_parsed", parsed.external_url) in service.calls


async def test_apply_command_with_foreign_url_hints_to_paste_text() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_message("/apply https://example.com/job"))
    assert service.calls == []
    assert "Projektbeschreibung" in telegram.texts()[-1]


async def test_apply_command_without_argument_shows_usage() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_message("/apply"))
    assert service.calls == []
    assert "/apply" in telegram.texts()[-1]


async def test_reply_with_email_sets_recipient() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.by_message[55] = _view()
    await _bot(telegram, service).process_update(_message("neu@firma.de", reply_to=55))
    assert ("set_recipient", (1, "neu@firma.de")) in service.calls


async def test_reply_with_instruction_revises_draft() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    service.by_message[55] = _view()
    await _bot(telegram, service).process_update(_message("Bitte kürzer", reply_to=55))
    assert ("revise", (1, "Bitte kürzer")) in service.calls
    assert any("Bewerbungsentwurf" in text for text in telegram.texts())


async def test_reply_to_unknown_message_hints() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_message("Bitte kürzer", reply_to=999))
    assert service.calls == []
    assert "keinen Bewerbungsentwurf" in telegram.texts()[-1]


async def test_help_and_plain_messages() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    bot = _bot(telegram, service)
    await bot.process_update(_message("/help"))
    assert telegram.texts()[-1] == HELP_TEXT
    await bot.process_update(_message("hallo bot"))
    assert "/help" in telegram.texts()[-1]
    assert service.calls == []


async def test_run_forever_advances_offset_and_stops() -> None:
    service = _FakeService()
    stop = asyncio.Event()
    seen_offsets: list[int | None] = []

    class _PollingTelegram(_FakeTelegram):
        async def get_updates(
            self, *, offset: int | None = None, timeout_s: int = 25
        ) -> list[TelegramUpdate] | None:
            seen_offsets.append(offset)
            if len(seen_offsets) == 1:
                return [_callback("apply:7", query_id="cb-poll")]
            stop.set()
            return []

    telegram = _PollingTelegram()
    await _bot(telegram, service).run_forever(stop=stop)
    # None -> after batch offset 2 -> final shutdown ack with offset 2
    assert seen_offsets == [None, 2, 2]
    assert ("draft_for_listing", 7) in service.calls


async def test_applyfoo_is_not_treated_as_apply_command() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    await _bot(telegram, service).process_update(_message("/applyfoo bar"))
    assert service.calls == []
    assert "keinen Kontext" in telegram.texts()[-1]


async def test_failed_draft_message_send_notifies_user() -> None:
    service = _FakeService()

    class _FailingSendTelegram(_FakeTelegram):
        async def send(
            self,
            text: str,
            *,
            disable_preview: bool = True,
            reply_markup: dict[str, object] | None = None,
        ) -> int | None:
            self.sent.append((text, reply_markup))
            return None

    telegram = _FailingSendTelegram()
    await _bot(telegram, service).process_update(_callback("apply:7"))
    assert service.recorded == []
    assert "konnte nicht angezeigt werden" in telegram.texts()[-1]


async def test_reply_to_older_draft_render_routes_via_memory_index() -> None:
    telegram, service = _FakeTelegram(), _FakeService()
    bot = _bot(telegram, service)
    await bot.process_update(_callback("apply:7"))  # draft shown as message 101
    # The DB lookup (fake) knows nothing; the in-memory index must still route it.
    await bot.process_update(_message("Bitte kürzer", reply_to=101))
    assert ("revise", (1, "Bitte kürzer")) in service.calls
