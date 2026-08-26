"""The bot process: parsing, the whitelist, routing by thread, chunking, approvals."""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot import telegram_bot
from project_pilot.agent import AgentReply, Approve, allow_everything
from project_pilot.models import Listing
from project_pilot.repository import Repository
from project_pilot.telegram_bot import (
    ALLOW,
    CHUNK_CHARS,
    DENY,
    NO_THREAD,
    Incoming,
    TelegramBot,
    chunk,
    parse_callbacks,
    parse_updates,
    question_text,
)

BOT_TOKEN = "123456:AAtest"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHAT_ID = "-1001234567890"
ME = 4242
THREAD = 77


class _FakeAgent:
    """Records what it was asked and answers with whatever it was given."""

    def __init__(
        self,
        *,
        text: str = "Antwort.",
        ok: bool = True,
        session: str | None = "sess-1",
        asks: list[tuple[str, str]] | None = None,
    ) -> None:
        self.text = text
        self.ok = ok
        self.session = session
        self.asks = asks or []
        self.approvals: list[bool] = []
        self.calls: list[tuple[int, str | None, str]] = []

    async def reply(
        self,
        *,
        listing_id: int,
        session_id: str | None,
        message: str,
        approve: Approve = allow_everything,
    ) -> AgentReply:
        self.calls.append((listing_id, session_id, message))
        for tool, detail in self.asks:
            self.approvals.append(await approve(tool, detail))
        return AgentReply(text=self.text, ok=self.ok, session_id=self.session)


def _update(
    update_id: int = 1,
    *,
    text: str = "passt das?",
    user_id: int = ME,
    thread_id: int | None = THREAD,
    chat_id: str = CHAT_ID,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id,
        "from": {"id": user_id},
        "chat": {"id": int(chat_id)},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"update_id": update_id, "message": message}


def _bot(session_factory: async_sessionmaker[AsyncSession], agent: _FakeAgent) -> TelegramBot:
    return TelegramBot(
        bot_token=BOT_TOKEN,
        chat_id=CHAT_ID,
        allowed_user_ids=[ME],
        agent=agent,  # type: ignore[arg-type]
        session_factory=session_factory,
    )


async def _wait_for(check: Callable[[], bool], *, tries: int = 200) -> None:
    """Give the dispatched answer a turn to reach the point being tested."""
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def _poll(bot: TelegramBot, client: httpx.AsyncClient) -> int:
    """Poll once and wait for the answers it dispatched (tests want them done)."""
    taken = await bot.poll_once(client)
    await bot.drain()
    return taken


async def _seed_thread(
    session_factory: async_sessionmaker[AsyncSession], *, thread_id: int = THREAD
) -> int:
    async with session_factory() as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(
            Listing(
                source="freelancermap",
                external_url=f"https://example.test/{thread_id}",
                url_hash=f"h{thread_id}",
                title="T",
            )
        )
        await repo.record_thread(listing.id, thread_id)
        await session.commit()
        return listing.id


def test_parse_updates_keeps_only_typed_messages() -> None:
    payload = {
        "result": [
            _update(1),
            {"update_id": 2, "edited_message": {"text": "nope"}},
            {"update_id": 3, "message": {"chat": {"id": 1}, "from": {"id": 1}}},  # no text
            {"update_id": 4, "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "  "}},
        ]
    }
    parsed = parse_updates(payload)
    assert [m.update_id for m in parsed] == [1]
    assert parsed[0].thread_id == THREAD


def test_parse_updates_survives_junk() -> None:
    assert parse_updates({}) == []
    assert parse_updates({"result": "nonsense"}) == []


def test_chunk_splits_on_line_breaks() -> None:
    text = ("a" * 100 + "\n") * 60  # comfortably past the limit
    parts = chunk(text)
    assert len(parts) > 1
    assert all(len(part) <= CHUNK_CHARS for part in parts)
    assert "".join(part.replace("\n", "") for part in parts) == text.replace("\n", "")


def test_chunk_hard_splits_when_there_is_no_break() -> None:
    parts = chunk("x" * (CHUNK_CHARS * 2 + 10))
    assert all(len(part) <= CHUNK_CHARS for part in parts)
    assert "".join(parts) == "x" * (CHUNK_CHARS * 2 + 10)


@respx.mock
async def test_a_message_in_a_known_thread_is_answered_there(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"ok": True, "result": [_update(5)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(text="Guter Match.")
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 1

    assert agent.calls[0][0] == listing_id  # routed to the thread's listing
    payload = json.loads(send.calls.last.request.read())
    assert payload["message_thread_id"] == THREAD
    assert payload["text"] == "Guter Match."


@respx.mock
async def test_the_session_carries_over_to_the_next_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})
    agent = _FakeAgent()
    bot = _bot(session_factory, agent)

    async with httpx.AsyncClient() as client:
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, text="eins")]})
        await _poll(bot, client)
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(2, text="zwei")]})
        await _poll(bot, client)

    # The first call opens a session; the second continues it rather than
    # starting the topic over.
    assert [call[1] for call in agent.calls] == [None, "sess-1"]


@respx.mock
async def test_a_failed_turn_still_keeps_its_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The session exists either way; losing its id would restart the topic.
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(text="⚠️ kaputt", ok=False, session="sess-7")
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(THREAD)
        assert thread is not None
        assert thread.session_id == "sess-7"


@respx.mock
async def test_a_stranger_is_ignored_without_a_reply(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Answering would confirm the bot is here and spend tokens on a stranger.
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, user_id=999)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 0

    assert agent.calls == []
    assert send.call_count == 0


@respx.mock
async def test_a_message_outside_a_topic_gets_one_sentence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=None)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls == []
    assert json.loads(send.calls.last.request.read())["text"] == NO_THREAD


@respx.mock
async def test_an_unknown_topic_is_not_guessed_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A topic a human opened has no listing; answering it would invent context.
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=4321)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls == []
    assert json.loads(send.calls.last.request.read())["text"] == NO_THREAD


@respx.mock
async def test_the_offset_advances_past_dropped_updates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # An unacknowledged update is redelivered forever.
    route = respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(7, user_id=999)]})
    bot = _bot(session_factory, _FakeAgent())
    async with httpx.AsyncClient() as client:
        await _poll(bot, client)
        await _poll(bot, client)

    assert json.loads(route.calls.last.request.read())["offset"] == 8


@respx.mock
async def test_a_long_answer_is_chunked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(text="z" * (CHUNK_CHARS * 2))
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert send.call_count == 2
    assert all(len(json.loads(call.request.read())["text"]) <= CHUNK_CHARS for call in send.calls)


def test_incoming_is_narrowed_to_what_routing_needs() -> None:
    # The dataclass is the contract between parsing and routing.
    message = parse_updates({"result": [_update(9)]})[0]
    assert message == Incoming(
        update_id=9, chat_id=int(CHAT_ID), thread_id=THREAD, user_id=ME, text="passt das?"
    )


def _press(
    update_id: int, decision: str, request_id: str, *, user_id: int = ME
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user_id},
            "data": f"{decision}:{request_id}",
        },
    }


def test_parse_callbacks_keeps_only_well_formed_answers() -> None:
    payload = {
        "result": [
            _press(1, ALLOW, "abc"),
            _press(2, "nonsense", "abc"),
            {"update_id": 3, "callback_query": {"id": "x", "from": {"id": ME}, "data": "allow:"}},
            {"update_id": 4, "message": {"text": "not a press"}},
        ]
    }
    parsed = parse_callbacks(payload)
    assert [(p.update_id, p.decision, p.request_id) for p in parsed] == [(1, ALLOW, "abc")]


def test_question_text_names_the_call() -> None:
    assert question_text("Bash", "rm -rf /") == "🔐 Freigabe: Bash\nrm -rf /"
    assert question_text("Read", "") == "🔐 Freigabe: Read"


@respx.mock
async def test_a_tool_that_is_not_pre_approved_asks_in_the_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The press that answers the question arrives through the same getUpdates
    # loop, so the answer must not be awaited inside it.
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    answered = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 500}}
    )
    agent = _FakeAgent(asks=[("Bash", "ls -la")])
    bot = _bot(session_factory, agent)

    async with httpx.AsyncClient() as client:
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
        await bot.poll_once(client)  # dispatches; the run stops on the question
        await _wait_for(lambda: sent.call_count > 0)
        question = json.loads(sent.calls.last.request.read())
        request_id = question["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[
            1
        ]
        respx.post(f"{API}/getUpdates").respond(
            200, json={"result": [_press(2, ALLOW, request_id)]}
        )
        await bot.poll_once(client)
        await bot.drain()

    assert question["text"] == "🔐 Freigabe: Bash\nls -la"
    assert question["message_thread_id"] == THREAD
    assert agent.approvals == [True]
    assert answered.call_count == 1


@respx.mock
async def test_a_refused_tool_comes_back_as_a_no(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    edited = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 500}}
    )
    agent = _FakeAgent(asks=[("Write", "/etc/passwd")])
    bot = _bot(session_factory, agent)

    async with httpx.AsyncClient() as client:
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
        await bot.poll_once(client)
        await _wait_for(lambda: sent.call_count > 0)
        data = json.loads(sent.calls.last.request.read())
        request_id = data["reply_markup"]["inline_keyboard"][0][1]["callback_data"].split(":")[1]
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(2, DENY, request_id)]})
        await bot.poll_once(client)
        await bot.drain()

    assert agent.approvals == [False]
    # The question is rewritten into its answer, so no live buttons are left.
    assert "abgelehnt" in json.loads(edited.calls.last.request.read())["text"]


@respx.mock
async def test_a_stranger_cannot_answer_a_permission_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 500}}
    )
    agent = _FakeAgent(asks=[("Bash", "whoami")])
    bot = _bot(session_factory, agent)

    async with httpx.AsyncClient() as client:
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
        await bot.poll_once(client)
        await _wait_for(lambda: sent.call_count > 0)
        data = json.loads(sent.calls.last.request.read())
        request_id = data["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[1]
        respx.post(f"{API}/getUpdates").respond(
            200, json={"result": [_press(2, ALLOW, request_id, user_id=999)]}
        )
        assert await bot.poll_once(client) == 0
        # Still waiting: a stranger's press is not an answer.
        assert agent.approvals == []
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(3, DENY, request_id)]})
        await bot.poll_once(client)
        await bot.drain()

    assert agent.approvals == [False]


@respx.mock
async def test_an_unanswered_question_refuses_instead_of_waiting(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A question nobody answers must not hold the turn open forever, and it must
    # not fall open either.
    monkeypatch.setattr(telegram_bot, "APPROVAL_TIMEOUT_S", 0.05)
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    edited = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 7}})

    agent = _FakeAgent(asks=[("Bash", "sleep 1")])
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.approvals == [False]
    assert "abgelehnt" in json.loads(edited.calls.last.request.read())["text"]


@respx.mock
async def test_a_question_telegram_refuses_counts_as_a_no(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # No question means no consent; allowing anyway would defeat the point.
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(400, json={"ok": False})

    agent = _FakeAgent(asks=[("Bash", "whoami")])
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.approvals == [False]
