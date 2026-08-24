"""The bot process: parsing, the whitelist, routing by thread, chunking."""

import json

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.agent import AgentReply
from project_pilot.models import Listing
from project_pilot.repository import Repository
from project_pilot.telegram_bot import (
    CHUNK_CHARS,
    NO_THREAD,
    Incoming,
    TelegramBot,
    chunk,
    parse_updates,
)

BOT_TOKEN = "123456:AAtest"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHAT_ID = "-1001234567890"
ME = 4242
THREAD = 77


class _FakeAgent:
    """Records what it was asked and answers with whatever it was given."""

    def __init__(self, *, text: str = "Antwort.", ok: bool = True) -> None:
        self.text = text
        self.ok = ok
        self.calls: list[tuple[int, list[dict[str, str]], str]] = []

    async def reply(
        self, *, listing_id: int, history: list[dict[str, str]], message: str
    ) -> AgentReply:
        self.calls.append((listing_id, history, message))
        return AgentReply(text=self.text, ok=self.ok)


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
        assert await _bot(session_factory, agent).poll_once(client) == 1

    assert agent.calls[0][0] == listing_id  # routed to the thread's listing
    payload = json.loads(send.calls.last.request.read())
    assert payload["message_thread_id"] == THREAD
    assert payload["text"] == "Guter Match."


@respx.mock
async def test_the_turn_is_remembered_for_the_next_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_thread(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})
    agent = _FakeAgent()
    bot = _bot(session_factory, agent)

    async with httpx.AsyncClient() as client:
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, text="eins")]})
        await bot.poll_once(client)
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(2, text="zwei")]})
        await bot.poll_once(client)

    # The second call sees the first exchange.
    assert [turn["text"] for turn in agent.calls[1][1]] == ["eins", "Antwort."]


@respx.mock
async def test_a_failed_turn_is_not_remembered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The next message should start from the last state that made sense.
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(text="⚠️ kaputt", ok=False)
    async with httpx.AsyncClient() as client:
        await _bot(session_factory, agent).poll_once(client)

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(THREAD)
        assert thread is not None
        assert thread.history == []


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
        assert await _bot(session_factory, agent).poll_once(client) == 0

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
        await _bot(session_factory, agent).poll_once(client)

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
        await _bot(session_factory, agent).poll_once(client)

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
        await bot.poll_once(client)
        await bot.poll_once(client)

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
        await _bot(session_factory, agent).poll_once(client)

    assert send.call_count == 2
    assert all(len(json.loads(call.request.read())["text"]) <= CHUNK_CHARS for call in send.calls)


def test_incoming_is_narrowed_to_what_routing_needs() -> None:
    # The dataclass is the contract between parsing and routing.
    message = parse_updates({"result": [_update(9)]})[0]
    assert message == Incoming(
        update_id=9, chat_id=int(CHAT_ID), thread_id=THREAD, user_id=ME, text="passt das?"
    )
