"""The bot process: parsing, the whitelist, routing by thread, chunking, approvals."""

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot import telegram_bot
from project_pilot.agent import AgentReply, Approve, Progress, allow_everything, report_nothing
from project_pilot.mcp_prompts import PROMPTS
from project_pilot.models import Evaluation, EvaluationStage, Listing, Verdict
from project_pilot.repository import Repository
from project_pilot.telegram_bot import (
    ALLOW,
    CHUNK_CHARS,
    COMMANDS,
    DENY,
    DONE,
    GENERAL,
    NO_DESCRIPTION,
    OPEN_SLOT,
    SEEN,
    TOPIC_NAME_CHARS,
    UNNAMED_TOPIC,
    Incoming,
    TelegramBot,
    chunk,
    expand_command,
    parse_callbacks,
    parse_updates,
    question_text,
    topic_name,
    update_ids,
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
        steps: list[str] | None = None,
        acted_on: int | None = None,
    ) -> None:
        self.text = text
        self.ok = ok
        self.session = session
        self.asks = asks or []
        self.steps = steps or []
        self.acted_on = acted_on
        self.approvals: list[bool] = []
        self.calls: list[tuple[int | None, str | None, str]] = []

    async def reply(
        self,
        *,
        listing_id: int | None,
        session_id: str | None,
        message: str,
        approve: Approve = allow_everything,
        progress: Progress = report_nothing,
    ) -> AgentReply:
        self.calls.append((listing_id, session_id, message))
        for tool, detail in self.asks:
            self.approvals.append(await approve(tool, detail))
        for label in self.steps:
            await progress(label)
        return AgentReply(
            text=self.text, ok=self.ok, session_id=self.session, listing_id=self.acted_on
        )


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


def _cosmetic() -> None:
    """Stub the endpoints that only decorate a turn.

    Only the ones no test sets itself: respx keys routes by pattern, so
    re-registering one a test already configured would silently overwrite its
    expectation.
    """
    for method in ("setMessageReaction", "deleteMessage", "sendChatAction"):
        respx.post(f"{API}/{method}").respond(200, json={"ok": True})


async def _wait_for(check: Callable[[], bool], *, tries: int = 200) -> None:
    """Give the dispatched answer a turn to reach the point being tested."""
    for _ in range(tries):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def _poll(bot: TelegramBot, client: httpx.AsyncClient) -> int:
    """Poll once and wait for the answers it dispatched (tests want them done)."""
    _cosmetic()
    taken = await bot.poll_once(client)
    await bot.drain()
    return taken


async def _seed_listing(
    session_factory: async_sessionmaker[AsyncSession], *, score: int = 87
) -> int:
    """A stored listing with the LLM verdict a card is rendered from."""
    async with session_factory() as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(
            Listing(
                source="freelancermap",
                external_url="https://example.test/checked",
                url_hash="hchecked",
                title="Senior Python Developer",
                raw={"company": "ACME GmbH"},
            )
        )
        await repo.add_evaluation(
            Evaluation(
                listing_id=listing.id,
                stage=EvaluationStage.LLM,
                verdict=Verdict.MATCH,
                score=score,
                reason={"reasons": ["Stack passt"], "risk_flags": []},
            )
        )
        await session.commit()
        return listing.id


async def _seed_thread(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: int = THREAD,
    description: str = "",
) -> int:
    async with session_factory() as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(
            Listing(
                source="freelancermap",
                external_url=f"https://example.test/{thread_id}",
                url_hash=f"h{thread_id}",
                title="T",
                description=description,
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
async def test_a_message_in_the_main_area_gets_a_thread_of_its_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Otherwise the answer, its steps and the next thing you drop there all
    # interleave in one growing chat.
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_update(1, thread_id=None, text="prüf mal https://x.test/p")]}
    )
    opened = respx.post(f"{API}/createForumTopic").respond(
        200, json={"ok": True, "result": {"message_thread_id": 8080}}
    )
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert json.loads(opened.calls.last.request.read())["name"] == "prüf mal https://x.test/p"
    posted = [json.loads(call.request.read()) for call in send.calls]
    # A pointer stays in the main area, so the new thread is one tap away.
    assert posted[0]["text"] == "→ prüf mal https://x.test/p"
    assert "message_thread_id" not in posted[0]
    assert posted[0]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith("/8080")
    # What was written is repeated in the thread, so it reads on its own.
    assert posted[1]["text"] == "prüf mal https://x.test/p"
    assert all(entry["message_thread_id"] == 8080 for entry in posted[1:])
    assert agent.calls[0][0] is None  # no listing yet — the agent ingests it

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(8080)
        assert thread is not None
        assert thread.session_id == "sess-1"


@respx.mock
async def test_a_chat_that_cannot_hold_topics_is_answered_where_it_is(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Not a forum, or no Manage Topics: answering in place beats dropping it.
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=None)]})
    respx.post(f"{API}/createForumTopic").respond(400, json={"ok": False})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert json.loads(send.calls.last.request.read())["text"] == "Antwort."
    assert "message_thread_id" not in json.loads(send.calls.last.request.read())

    async with session_factory() as session:
        assert await Repository(session).get_thread_by_thread_id(GENERAL) is not None


def test_a_topic_is_named_after_what_was_written() -> None:
    assert topic_name("prüf mal das hier") == "prüf mal das hier"
    # The command that started it is not what the thread is about.
    assert topic_name("/check_project@a_bot Senior Python Dev") == "Senior Python Dev"
    assert topic_name("/check_project") == UNNAMED_TOPIC
    long = topic_name("x" * 200)
    assert len(long) <= TOPIC_NAME_CHARS + 2
    assert long.endswith("…")


@respx.mock
async def test_a_topic_you_opened_yourself_is_answered_without_a_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # This is how you bring your own project description: open a topic, paste
    # it, and the agent ingests it rather than refusing to talk.
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=4321)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] is None
    assert json.loads(send.calls.last.request.read())["message_thread_id"] == 4321

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(4321)
        assert thread is not None
        assert thread.listing_id is None


@respx.mock
async def test_a_command_in_a_listingless_topic_names_what_is_still_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_update(1, thread_id=4321, text="/check_project")]}
    )
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert OPEN_SLOT in agent.calls[0][2]
    assert "{listing}" not in agent.calls[0][2]


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
        update_id=9,
        chat_id=int(CHAT_ID),
        thread_id=THREAD,
        user_id=ME,
        text="passt das?",
        # Carried so the answer can react on the very message it answers.
        message_id=9,
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
    assert [(p.update_id, p.action, p.argument) for p in parsed] == [(1, ALLOW, "abc")]


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
        _cosmetic()
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
        _cosmetic()
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
        _cosmetic()
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


@respx.mock
async def test_the_command_menu_is_published_on_start(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The menu is also the way in that survives the bot's privacy mode, since a
    # command is always delivered to a bot.
    route = respx.post(f"{API}/setMyCommands").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        assert await _bot(session_factory, _FakeAgent()).register_commands(client) is True

    published = json.loads(route.calls.last.request.read())["commands"]
    # The menu is the MCP prompt list, not a second list maintained here.
    assert [entry["command"] for entry in published] == list(PROMPTS)
    assert [entry["description"] for entry in published] == [
        description for description, _body in PROMPTS.values()
    ]
    assert [name for name, _ in COMMANDS] == list(PROMPTS)


@respx.mock
async def test_a_refused_command_menu_does_not_stop_the_bot(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    respx.post(f"{API}/setMyCommands").respond(400, json={"ok": False})

    async with httpx.AsyncClient() as client:
        assert await _bot(session_factory, _FakeAgent()).register_commands(client) is False


def test_a_command_expands_into_its_mcp_workflow() -> None:
    # What reaches the agent is the prompt body the MCP server serves, so the
    # bot runs the same procedure as every other surface.
    expanded = expand_command("/write_application")
    assert expanded is not None
    assert expanded.startswith("Draft an application for this listing:")
    assert "project_pilot_draft_application" in expanded


def test_a_command_keeps_what_was_typed_after_it() -> None:
    expanded = expand_command("/write_application@project_pilot_bot kürzer bitte")
    assert expanded is not None
    assert expanded.endswith("kürzer bitte")


def test_plain_text_and_unknown_commands_are_left_alone() -> None:
    assert expand_command("prüf das mal") is None
    assert expand_command("/nonsense") is None


@respx.mock
async def test_a_command_reaches_the_agent_as_the_workflow_body(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_update(1, text="/check_project")]}
    )
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 1}})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    sent = agent.calls[0][2]
    assert sent.startswith("Judge whether this project listing is a genuine match")
    # The slot is filled with the topic's own listing, not left as a placeholder.
    assert f"Listing {listing_id}" in sent
    assert "{listing}" not in sent


def _card_press(
    update_id: int, action: str, listing_id: int, *, user_id: int = ME
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user_id},
            "data": f"{action}:{listing_id}",
            "message": {"message_id": 900, "message_thread_id": THREAD},
        },
    }


@respx.mock
async def test_describe_posts_the_listing_text_into_the_topic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The card leaves the description out; this button is where it lives.
    listing_id = await _seed_thread(session_factory, description="Volltext der Ausschreibung.")
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "describe", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, _FakeAgent()), client) == 1

    payload = json.loads(sent.calls.last.request.read())
    assert payload["text"] == "Volltext der Ausschreibung."
    assert payload["message_thread_id"] == THREAD


@respx.mock
async def test_describe_says_so_when_there_is_no_description(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "describe", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    assert json.loads(sent.calls.last.request.read())["text"] == NO_DESCRIPTION


@respx.mock
async def test_decline_closes_the_topic_and_takes_the_buttons_away(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Closed, not deleted: what was offered and turned down stays readable.
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "decline", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 5}})
    cleared = respx.post(f"{API}/editMessageReplyMarkup").respond(200, json={"ok": True})
    closed = respx.post(f"{API}/closeForumTopic").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert cleared.call_count == 1
    assert json.loads(closed.calls.last.request.read())["message_thread_id"] == THREAD
    assert agent.calls == []  # declining costs no tokens


@respx.mock
async def test_accept_starts_the_drafting_workflow_in_the_topic(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "accept", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    respx.post(f"{API}/editMessageReplyMarkup").respond(200, json={"ok": True})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 5}})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] == listing_id
    assert agent.calls[0][2].startswith("Draft an application for this listing:")
    assert f"Listing {listing_id}" in agent.calls[0][2]


@respx.mock
async def test_a_stranger_cannot_decide_a_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "accept", listing_id, user_id=999)]}
    )
    answered = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 5}})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 0

    assert agent.calls == []
    assert answered.call_count == 0


@respx.mock
async def test_accept_works_on_a_card_in_the_general_area(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # test-match puts its card there; General holds a conversation like any topic.
    listing_id = await _seed_thread(session_factory)
    press = _card_press(1, "accept", listing_id)
    press["callback_query"]["message"] = {"message_id": 900}  # type: ignore[index]
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [press]})
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    respx.post(f"{API}/editMessageReplyMarkup").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] == listing_id
    assert agent.calls[0][2].startswith("Draft an application for this listing:")
    assert "message_thread_id" not in json.loads(sent.calls.last.request.read())


@respx.mock
async def test_a_message_is_marked_seen_and_then_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The turn can run for minutes; the reaction is the instant "I have it".
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 2}})
    reacted = respx.post(f"{API}/setMessageReaction").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    emojis = [json.loads(call.request.read())["reaction"] for call in reacted.calls]
    assert emojis == [[{"type": "emoji", "emoji": SEEN}], [{"type": "emoji", "emoji": DONE}]]
    assert json.loads(reacted.calls.last.request.read())["message_id"] == 1


@respx.mock
async def test_a_failed_turn_clears_the_reaction_rather_than_marking_it_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A thumb up over an error message would be a lie.
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 2}})
    reacted = respx.post(f"{API}/setMessageReaction").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent(text="⚠️ kaputt", ok=False)), client)

    assert json.loads(reacted.calls.last.request.read())["reaction"] == []


@respx.mock
async def test_each_step_moves_one_status_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One line that changes, not a wall of status messages.
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 55}}
    )
    edited = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    deleted = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    steps = ["prüfe das Listing gegen dein Profil", "schreibe die Bewerbung"]
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent(steps=steps)), client)

    # The first step opens the line, the second edits it.
    assert json.loads(sent.calls[0].request.read())["text"] == f"⏳ {steps[0]} …"
    assert json.loads(edited.calls.last.request.read())["text"] == f"⏳ {steps[1]} …"
    # And it is taken away once the answer is there.
    assert json.loads(deleted.calls.last.request.read())["message_id"] == 55


@respx.mock
async def test_the_same_step_twice_in_a_row_is_not_reported_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 55}})
    edited = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent(steps=["lese eine Datei"] * 3)), client)

    assert edited.call_count == 0


@respx.mock
async def test_the_card_appears_once_the_agent_takes_a_listing_on(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Paste a link in a thread of your own and you get the same card a scan
    # match gets, built from the stored verdict rather than written by the model.
    listing_id = await _seed_listing(session_factory, score=87)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=4321)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(acted_on=listing_id)
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    card = json.loads(send.calls[0].request.read())
    assert "🏢 Company: ACME GmbH" in card["text"]
    assert "🎯 Score: 87/100" in card["text"]
    assert [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row] == [
        f"accept:{listing_id}",
        f"decline:{listing_id}",
        f"describe:{listing_id}",
    ]
    # And the thread is now about that listing, so the next message knows it.
    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(4321)
        assert thread is not None
        assert thread.listing_id == listing_id


@respx.mock
async def test_the_card_is_not_repeated_for_a_thread_that_already_has_its_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A match's own topic was opened with its card; showing it again is noise.
    listing_id = await _seed_thread(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(acted_on=listing_id)
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert [json.loads(call.request.read())["text"] for call in send.calls] == ["Antwort."]


def test_update_ids_covers_what_neither_parser_takes() -> None:
    payload = {
        "result": [
            _update(1),
            {"update_id": 2, "message": {"forum_topic_created": {"name": "f"}}},
            {"update_id": 3, "edited_message": {"text": "x"}},
            {"nonsense": True},
        ]
    }
    assert update_ids(payload) == [1, 2, 3]


@respx.mock
async def test_an_update_nobody_acts_on_still_moves_the_offset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Telegram redelivers anything unconfirmed at once, so an update left
    # behind spins the poll loop forever and nothing newer is ever reached.
    service = {"update_id": 41, "message": {"forum_topic_created": {"name": "f"}}}
    updates = respx.post(f"{API}/getUpdates").respond(200, json={"result": [service]})

    bot = _bot(session_factory, _FakeAgent())
    async with httpx.AsyncClient() as client:
        assert await _poll(bot, client) == 0
        await bot.poll_once(client)

    assert json.loads(updates.calls.last.request.read())["offset"] == 42


@respx.mock
async def test_only_the_two_kinds_of_update_are_requested(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    updates = respx.post(f"{API}/getUpdates").respond(200, json={"result": []})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    asked = json.loads(updates.calls.last.request.read())
    assert asked["allowed_updates"] == ["message", "callback_query"]
