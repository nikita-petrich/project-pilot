"""The bot process: parsing, the two chats, routing by comment thread, approvals.

The architecture under test: a match is a post in the *channel*, Telegram
forwards it into the linked *discussion group* by itself, and the forwarded copy
roots the comment thread people write in. So almost every test here has two chat
ids in play, and the interesting failures are the ones that mix them up.
"""

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
    Forward,
    Incoming,
    TelegramBot,
    chunk,
    expand_command,
    parse_callbacks,
    parse_forwards,
    parse_updates,
    post_link,
    question_text,
    update_ids,
)

BOT_TOKEN = "123456:AAtest"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHANNEL = "-1001234567890"
GROUP = "-1009876543210"
ME = 4242
# The card's id in the channel, and the id of Telegram's forwarded copy in the
# discussion group — which is what every comment carries as its thread id.
POST = 900
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
    chat_id: str = GROUP,
) -> dict[str, object]:
    """A message someone typed in the discussion group."""
    message: dict[str, object] = {
        "message_id": update_id,
        "from": {"id": user_id},
        "chat": {"id": int(chat_id)},
        "text": text,
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"update_id": update_id, "message": message}


def _forward(
    update_id: int = 1,
    *,
    channel_message_id: int = POST,
    root_id: int = THREAD,
    chat_id: str = GROUP,
) -> dict[str, object]:
    """Telegram's own copy of a channel post, landing in the discussion group."""
    return {
        "update_id": update_id,
        "message": {
            "message_id": root_id,
            "chat": {"id": int(chat_id)},
            "sender_chat": {"id": int(CHANNEL), "type": "channel"},
            "is_automatic_forward": True,
            "forward_origin": {
                "type": "channel",
                "chat": {"id": int(CHANNEL), "type": "channel"},
                "message_id": channel_message_id,
            },
            "text": "⭐ 87 · Senior Python Developer · ACME GmbH",
        },
    }


def _press(
    update_id: int, decision: str, request_id: str, *, user_id: int = ME
) -> dict[str, object]:
    """A permission answer — those buttons live in the discussion group."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user_id},
            "data": f"{decision}:{request_id}",
            "message": {"message_id": 500, "chat": {"id": int(GROUP)}},
        },
    }


def _card_press(
    update_id: int,
    action: str,
    listing_id: int,
    *,
    user_id: int = ME,
    message_id: int = POST,
) -> dict[str, object]:
    """A decision on a match card — those buttons live on the channel post."""
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": {"id": user_id},
            "data": f"{action}:{listing_id}",
            "message": {"message_id": message_id, "chat": {"id": int(CHANNEL)}},
        },
    }


def _bot(
    session_factory: async_sessionmaker[AsyncSession],
    agent: _FakeAgent,
    *,
    group: str | None = GROUP,
) -> TelegramBot:
    return TelegramBot(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL,
        allowed_user_ids=[ME],
        agent=agent,  # type: ignore[arg-type]
        session_factory=session_factory,
        group_chat_id=group,
    )


def _sent(route: respx.Route, index: int = -1) -> dict[str, object]:
    payload = json.loads(route.calls[index].request.read())
    assert isinstance(payload, dict)
    return payload


def _replied_to(payload: dict[str, object]) -> int | None:
    """The message a send hangs under — how a comment thread is addressed."""
    params = payload.get("reply_parameters")
    if not isinstance(params, dict):
        return None
    target = params.get("message_id")
    return target if isinstance(target, int) else None


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


async def _seed_card(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    channel_message_id: int = POST,
    thread_id: int | None = THREAD,
    description: str = "",
) -> int:
    """A match that has been posted, and whose comment thread is already known."""
    async with session_factory() as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(
            Listing(
                source="freelancermap",
                external_url=f"https://example.test/{channel_message_id}",
                url_hash=f"h{channel_message_id}",
                title="T",
                description=description,
            )
        )
        thread = await repo.record_channel_message(listing.id, channel_message_id)
        if thread_id is not None:
            await repo.bind_thread_id(thread, thread_id)
        await session.commit()
        return listing.id


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


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


def test_the_automatic_forward_is_never_read_as_a_human_message() -> None:
    """Its text is the card itself — answering it would have the bot talk to itself."""
    assert parse_updates({"result": [_forward(1)]}) == []


def test_parse_updates_survives_junk() -> None:
    assert parse_updates({}) == []
    assert parse_updates({"result": "nonsense"}) == []


def test_parse_forwards_ties_a_channel_post_to_its_comment_thread() -> None:
    parsed = parse_forwards({"result": [_forward(3, channel_message_id=12, root_id=34)]})
    assert parsed == [Forward(update_id=3, chat_id=int(GROUP), channel_message_id=12, root_id=34)]


def test_parse_forwards_ignores_everything_that_is_not_one() -> None:
    payload = {
        "result": [
            _update(1),  # a human's message
            # A forward from a person, not the automatic one from a channel.
            {
                "update_id": 2,
                "message": {
                    "message_id": 9,
                    "chat": {"id": int(GROUP)},
                    "is_automatic_forward": True,
                    "forward_origin": {"type": "user", "sender_user": {"id": 1}},
                },
            },
            # Automatic, from a channel, but with no origin message to key on.
            {
                "update_id": 3,
                "message": {
                    "message_id": 9,
                    "chat": {"id": int(GROUP)},
                    "is_automatic_forward": True,
                    "forward_origin": {"type": "channel"},
                },
            },
        ]
    }
    assert parse_forwards(payload) == []
    assert parse_forwards({}) == []


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


def test_a_press_carries_the_chat_it_happened_in() -> None:
    """Card decisions arrive from the channel, permission answers from the group."""
    card = parse_callbacks({"result": [_card_press(1, "accept", 5)]})[0]
    assert (card.chat_id, card.message_id) == (int(CHANNEL), POST)
    permission = parse_callbacks({"result": [_press(2, ALLOW, "abc")]})[0]
    assert permission.chat_id == int(GROUP)


def test_incoming_is_narrowed_to_what_routing_needs() -> None:
    # The dataclass is the contract between parsing and routing.
    message = parse_updates({"result": [_update(9)]})[0]
    assert message == Incoming(
        update_id=9,
        chat_id=int(GROUP),
        thread_id=THREAD,
        user_id=ME,
        text="passt das?",
        # Carried so the answer can react on the very message it answers.
        message_id=9,
    )


def test_update_ids_covers_what_neither_parser_takes() -> None:
    payload = {
        "result": [
            _update(1),
            {"update_id": 2, "message": {"new_chat_members": []}},
            {"update_id": 3, "edited_message": {"text": "x"}},
            {"nonsense": True},
        ]
    }
    assert update_ids(payload) == [1, 2, 3]


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


def test_post_link_addresses_a_private_channel_and_nothing_else() -> None:
    assert post_link(CHANNEL, 12) == "https://t.me/c/1234567890/12"
    # A public @name or a personal chat has no /c/ form; no button beats a
    # button that goes nowhere.
    assert post_link("@somechannel", 12) is None


def test_question_text_names_the_call() -> None:
    assert question_text("Bash", "rm -rf /") == "🔐 Freigabe: Bash\nrm -rf /"
    assert question_text("Read", "") == "🔐 Freigabe: Read"


# --------------------------------------------------------------------------
# Finding the discussion group
# --------------------------------------------------------------------------


@respx.mock
async def test_the_discussion_group_is_read_off_the_channel(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Telegram already knows which group is linked; a second setting could only
    # disagree with it.
    route = respx.post(f"{API}/getChat").respond(
        200, json={"ok": True, "result": {"id": int(CHANNEL), "linked_chat_id": int(GROUP)}}
    )
    bot = _bot(session_factory, _FakeAgent(), group=None)

    async with httpx.AsyncClient() as client:
        assert await bot.resolve_group(client) == GROUP
        assert await bot.resolve_group(client) == GROUP  # cached, not asked twice

    assert route.call_count == 1
    assert json.loads(route.calls.last.request.read())["chat_id"] == CHANNEL


@respx.mock
async def test_a_channel_without_a_discussion_group_is_reported_not_guessed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    respx.post(f"{API}/getChat").respond(200, json={"ok": True, "result": {"id": int(CHANNEL)}})
    bot = _bot(session_factory, _FakeAgent(), group=None)

    async with httpx.AsyncClient() as client:
        assert await bot.resolve_group(client) is None


@respx.mock
async def test_the_group_is_asked_for_again_after_a_failure(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A bot that came up before the group was linked must not stay deaf until
    # someone restarts it.
    route = respx.post(f"{API}/getChat")
    route.side_effect = [
        httpx.Response(500),
        httpx.Response(
            200, json={"ok": True, "result": {"id": int(CHANNEL), "linked_chat_id": int(GROUP)}}
        ),
    ]
    bot = _bot(session_factory, _FakeAgent(), group=None)

    async with httpx.AsyncClient() as client:
        assert await bot.resolve_group(client) is None
        assert await bot.resolve_group(client) == GROUP


@respx.mock
async def test_without_a_group_no_message_is_answered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Answering a chat that may not be the linked group would be guessing.
    respx.post(f"{API}/getChat").respond(500)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent, group=None), client) == 0

    assert agent.calls == []
    assert send.call_count == 0


# --------------------------------------------------------------------------
# The automatic forward, which is what makes a card routable
# --------------------------------------------------------------------------


@respx.mock
async def test_the_forward_binds_the_comment_thread_to_the_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_card(session_factory, thread_id=None)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_forward(1)]})

    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, _FakeAgent()), client) == 1

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(THREAD)
        assert thread is not None
        assert thread.listing_id == listing_id
        assert thread.channel_message_id == POST


@respx.mock
async def test_a_redelivered_forward_changes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory, thread_id=None)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_forward(1), _forward(2)]})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(THREAD)
        assert thread is not None


@respx.mock
async def test_a_forward_for_a_post_we_did_not_send_is_skipped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Someone else's channel post, or one whose row was declined away.
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_forward(1, channel_message_id=4711)]}
    )

    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, _FakeAgent()), client) == 0

    async with session_factory() as session:
        assert await Repository(session).get_thread_by_thread_id(THREAD) is None


@respx.mock
async def test_a_forward_into_another_chat_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory, thread_id=None)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_forward(1, chat_id="-1005555555555")]}
    )

    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, _FakeAgent()), client) == 0


@respx.mock
async def test_a_forward_is_bound_before_a_comment_in_the_same_round_is_routed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Both can arrive in one getUpdates response; order decides whether the
    # first thing you type in a brand-new thread reaches its listing.
    listing_id = await _seed_card(session_factory, thread_id=None)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_forward(1), _update(2, text="passt das?")]}
    )
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 5}})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] == listing_id


# --------------------------------------------------------------------------
# Answering in the comment thread
# --------------------------------------------------------------------------


@respx.mock
async def test_a_comment_is_answered_in_its_own_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"ok": True, "result": [_update(5)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(text="Guter Match.")
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 1

    assert agent.calls[0][0] == listing_id  # routed to the thread's listing
    payload = _sent(send)
    assert payload["chat_id"] == GROUP  # the group, never the channel
    assert payload["text"] == "Guter Match."
    # A discussion group has no forum topics: the thread is addressed by
    # replying to its root, not by a message_thread_id.
    assert _replied_to(payload) == THREAD
    assert "message_thread_id" not in payload


@respx.mock
async def test_a_reply_survives_a_root_that_was_deleted_mid_turn(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(5)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    params = _sent(send)["reply_parameters"]
    assert isinstance(params, dict)
    # Losing the thread is acceptable; losing the answer is not.
    assert params["allow_sending_without_reply"] is True


@respx.mock
async def test_the_session_carries_over_to_the_next_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory)
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
    # starting the thread over.
    assert [call[1] for call in agent.calls] == [None, "sess-1"]


@respx.mock
async def test_a_failed_turn_still_keeps_its_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The session exists either way; losing its id would restart the thread.
    await _seed_card(session_factory)
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
    await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, user_id=999)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 0

    assert agent.calls == []
    assert send.call_count == 0


@respx.mock
async def test_a_message_from_another_chat_is_ignored(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_update(1, chat_id="-1007777777777")]}
    )
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, _FakeAgent()), client) == 0

    assert send.call_count == 0


@respx.mock
async def test_a_message_in_the_group_itself_is_answered_where_it_stands(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # This is how you bring your own project: write in the group, and the answer
    # hangs under what you wrote rather than floating free below it.
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_update(11, thread_id=None, text="prüf mal https://x.test/p")]}
    )
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    payload = _sent(send)
    assert payload["chat_id"] == GROUP
    assert _replied_to(payload) == 11  # the human's own message
    assert agent.calls[0][0] is None  # no listing yet — the agent ingests it

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(GENERAL)
        assert thread is not None
        assert thread.session_id == "sess-1"


@respx.mock
async def test_a_thread_with_no_listing_yet_is_answered_all_the_same(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A comment thread whose forward has not been seen: talk, do not refuse.
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=4321)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] is None
    assert _replied_to(_sent(send)) == 4321

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(4321)
        assert thread is not None
        assert thread.listing_id is None


@respx.mock
async def test_a_command_without_a_listing_names_what_is_still_missing(
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
async def test_an_update_nobody_acts_on_still_moves_the_offset(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Telegram redelivers anything unconfirmed at once, so an update left
    # behind spins the poll loop forever and nothing newer is ever reached.
    service = {"update_id": 41, "message": {"new_chat_members": []}}
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


@respx.mock
async def test_a_long_answer_is_chunked(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(text="z" * (CHUNK_CHARS * 2))
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert send.call_count == 2
    assert all(len(json.loads(call.request.read())["text"]) <= CHUNK_CHARS for call in send.calls)


# --------------------------------------------------------------------------
# Permission questions
# --------------------------------------------------------------------------


@respx.mock
async def test_a_tool_that_is_not_pre_approved_asks_in_the_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The press that answers the question arrives through the same getUpdates
    # loop, so the answer must not be awaited inside it.
    await _seed_card(session_factory)
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
        question = _sent(sent)
        markup = question["reply_markup"]
        assert isinstance(markup, dict)
        rows = markup["inline_keyboard"]
        assert isinstance(rows, list)
        request_id = rows[0][0]["callback_data"].split(":")[1]
        respx.post(f"{API}/getUpdates").respond(
            200, json={"result": [_press(2, ALLOW, request_id)]}
        )
        await bot.poll_once(client)
        await bot.drain()

    assert question["text"] == "🔐 Freigabe: Bash\nls -la"
    assert question["chat_id"] == GROUP  # asked where you are, not in the channel
    assert _replied_to(question) == THREAD
    assert agent.approvals == [True]
    assert answered.call_count == 1


@respx.mock
async def test_a_refused_tool_comes_back_as_a_no(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory)
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
        data = _sent(sent)
        markup = data["reply_markup"]
        assert isinstance(markup, dict)
        rows = markup["inline_keyboard"]
        assert isinstance(rows, list)
        request_id = rows[0][1]["callback_data"].split(":")[1]
        respx.post(f"{API}/getUpdates").respond(200, json={"result": [_press(2, DENY, request_id)]})
        await bot.poll_once(client)
        await bot.drain()

    assert agent.approvals == [False]
    # The question is rewritten into its answer, so no live buttons are left.
    answer = _sent(edited)
    assert isinstance(answer["text"], str)
    assert "abgelehnt" in answer["text"]
    assert answer["chat_id"] == GROUP


@respx.mock
async def test_a_stranger_cannot_answer_a_permission_question(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory)
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
        data = _sent(sent)
        markup = data["reply_markup"]
        assert isinstance(markup, dict)
        rows = markup["inline_keyboard"]
        assert isinstance(rows, list)
        request_id = rows[0][0]["callback_data"].split(":")[1]
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
    await _seed_card(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    edited = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 7}})

    agent = _FakeAgent(asks=[("Bash", "sleep 1")])
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.approvals == [False]
    text = _sent(edited)["text"]
    assert isinstance(text, str)
    assert "abgelehnt" in text


@respx.mock
async def test_a_question_telegram_refuses_counts_as_a_no(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # No question means no consent; allowing anyway would defeat the point.
    await _seed_card(session_factory)
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(400, json={"ok": False})

    agent = _FakeAgent(asks=[("Bash", "whoami")])
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.approvals == [False]


# --------------------------------------------------------------------------
# The `/` menu
# --------------------------------------------------------------------------


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
    listing_id = await _seed_card(session_factory)
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
    # The slot is filled with the thread's own listing, not left as a placeholder.
    assert f"Listing {listing_id}" in sent
    assert "{listing}" not in sent


# --------------------------------------------------------------------------
# The three decisions on a card
# --------------------------------------------------------------------------


@respx.mock
async def test_describe_posts_the_listing_text_into_the_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The card leaves the description out; this button is where it lives.
    listing_id = await _seed_card(session_factory, description="Volltext der Ausschreibung.")
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "describe", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, _FakeAgent()), client) == 1

    payload = _sent(sent)
    assert payload["text"] == "Volltext der Ausschreibung."
    # The press happened on the channel post; the answer belongs in its thread.
    assert payload["chat_id"] == GROUP
    assert _replied_to(payload) == THREAD


@respx.mock
async def test_describe_says_so_when_there_is_no_description(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "describe", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    sent = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    assert _sent(sent)["text"] == NO_DESCRIPTION


@respx.mock
async def test_decline_deletes_the_post_and_the_thread_and_forgets_both(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A turned-down project leaves nothing on screen; the verdict stays in the DB.
    listing_id = await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "decline", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    deleted = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        _cosmetic()
        assert await _bot(session_factory, agent).poll_once(client) == 1

    gone = [json.loads(call.request.read()) for call in deleted.calls]
    # The card in the channel, and the forwarded copy that roots the thread —
    # deleting that root is what makes the thread itself disappear.
    assert gone == [
        {"chat_id": CHANNEL, "message_id": POST},
        {"chat_id": GROUP, "message_id": THREAD},
    ]
    assert agent.calls == []  # declining costs no tokens

    async with session_factory() as session:
        repo = Repository(session)
        assert await repo.get_thread_by_thread_id(THREAD) is None
        assert await repo.get_thread_by_channel_message(POST) is None
        # The listing itself, and its verdict, are untouched.
        assert await repo.get_listing(listing_id) is not None


@respx.mock
async def test_decline_before_the_forward_lands_still_deletes_the_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_card(session_factory, thread_id=None)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "decline", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    deleted = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        _cosmetic()
        await _bot(session_factory, _FakeAgent()).poll_once(client)

    assert [json.loads(call.request.read()) for call in deleted.calls] == [
        {"chat_id": CHANNEL, "message_id": POST}
    ]


@respx.mock
async def test_accept_starts_the_drafting_workflow_in_the_thread(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "accept", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    cleared = respx.post(f"{API}/editMessageReplyMarkup").respond(200, json={"ok": True})
    respx.post(f"{API}/sendChatAction").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] == listing_id
    assert agent.calls[0][2].startswith("Draft an application for this listing:")
    assert f"Listing {listing_id}" in agent.calls[0][2]
    # The buttons come off the channel post, and the work happens in the thread.
    assert _sent(cleared) == {"chat_id": CHANNEL, "message_id": POST}
    assert _sent(send)["chat_id"] == GROUP
    assert _replied_to(_sent(send)) == THREAD


@respx.mock
async def test_accept_before_the_forward_lands_answers_in_the_group(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A few seconds at most, and an answer in the group beats no answer at all.
    listing_id = await _seed_card(session_factory, thread_id=None)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "accept", listing_id)]}
    )
    respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    respx.post(f"{API}/editMessageReplyMarkup").respond(200, json={"ok": True})
    send = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 5}}
    )

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert agent.calls[0][0] == listing_id
    assert _sent(send)["chat_id"] == GROUP
    assert _replied_to(_sent(send)) is None


@respx.mock
async def test_a_card_press_with_a_bad_listing_id_is_dropped(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    press = _card_press(1, "accept", 0)
    callback = press["callback_query"]
    assert isinstance(callback, dict)
    callback["data"] = "accept:nonsense"
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [press]})
    answered = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 0

    assert agent.calls == []
    assert answered.call_count == 0


@respx.mock
async def test_a_stranger_cannot_decide_a_card(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(
        200, json={"result": [_card_press(1, "accept", listing_id, user_id=999)]}
    )
    answered = respx.post(f"{API}/answerCallbackQuery").respond(200, json={"ok": True})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 5}})
    deleted = respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    agent = _FakeAgent()
    async with httpx.AsyncClient() as client:
        assert await _poll(_bot(session_factory, agent), client) == 0

    assert agent.calls == []
    assert answered.call_count == 0
    assert deleted.call_count == 0  # and nothing of his was deleted either


# --------------------------------------------------------------------------
# Progress: reactions and the status line
# --------------------------------------------------------------------------


@respx.mock
async def test_a_message_is_marked_seen_and_then_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The turn can run for minutes; the reaction is the instant "I have it".
    await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 2}})
    reacted = respx.post(f"{API}/setMessageReaction").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent()), client)

    marks = [json.loads(call.request.read()) for call in reacted.calls]
    assert [mark["reaction"] for mark in marks] == [
        [{"type": "emoji", "emoji": SEEN}],
        [{"type": "emoji", "emoji": DONE}],
    ]
    # On the message in the group, not on the card in the channel.
    assert {mark["chat_id"] for mark in marks} == {GROUP}
    assert marks[-1]["message_id"] == 1


@respx.mock
async def test_a_failed_turn_clears_the_reaction_rather_than_marking_it_done(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A thumb up over an error message would be a lie.
    await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 2}})
    reacted = respx.post(f"{API}/setMessageReaction").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent(text="⚠️ kaputt", ok=False)), client)

    assert _sent(reacted)["reaction"] == []


@respx.mock
async def test_each_step_moves_one_status_line(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # One line that changes, not a wall of status messages.
    await _seed_card(session_factory)
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
    assert _sent(sent, 0)["text"] == f"⏳ {steps[0]} …"
    assert _sent(edited)["text"] == f"⏳ {steps[1]} …"
    # And it is taken away once the answer is there — in the group.
    assert _sent(deleted) == {"chat_id": GROUP, "message_id": 55}


@respx.mock
async def test_the_same_step_twice_in_a_row_is_not_reported_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    respx.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {"message_id": 55}})
    edited = respx.post(f"{API}/editMessageText").respond(200, json={"ok": True})
    respx.post(f"{API}/deleteMessage").respond(200, json={"ok": True})

    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, _FakeAgent(steps=["lese eine Datei"] * 3)), client)

    assert edited.call_count == 0


# --------------------------------------------------------------------------
# A project you brought yourself gets a post of its own
# --------------------------------------------------------------------------


@respx.mock
async def test_a_listing_the_agent_takes_on_gets_its_own_channel_post(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Paste a link in the group and the project ends up exactly where a scanned
    # one does: one post in the channel, its own thread, the same three buttons.
    listing_id = await _seed_listing(session_factory, score=87)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=4321)]})
    send = respx.post(f"{API}/sendMessage").respond(
        200, json={"ok": True, "result": {"message_id": 4242}}
    )

    agent = _FakeAgent(acted_on=listing_id)
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    card = _sent(send, 0)
    assert card["chat_id"] == CHANNEL
    assert isinstance(card["text"], str)
    assert "🏢 Company: ACME GmbH" in card["text"]
    assert "🎯 Score: 87/100" in card["text"]
    markup = card["reply_markup"]
    assert isinstance(markup, dict)
    rows = markup["inline_keyboard"]
    assert isinstance(rows, list)
    assert [button["callback_data"] for row in rows for button in row] == [
        f"accept:{listing_id}",
        f"decline:{listing_id}",
        f"describe:{listing_id}",
    ]

    # A link back, so the conversation you are in is not a dead end.
    pointer = _sent(send, 1)
    assert pointer["chat_id"] == GROUP
    pointer_markup = pointer["reply_markup"]
    assert isinstance(pointer_markup, dict)
    pointer_rows = pointer_markup["inline_keyboard"]
    assert isinstance(pointer_rows, list)
    assert pointer_rows[0][0]["url"] == "https://t.me/c/1234567890/4242"

    async with session_factory() as session:
        repo = Repository(session)
        thread = await repo.get_thread_by_thread_id(4321)
        assert thread is not None
        assert thread.listing_id == listing_id
        # One row, carrying both: the conversation it started in and the post
        # that now represents it. A second row would split the match in two.
        assert thread.channel_message_id == 4242
        stored = await repo.get_thread_by_channel_message(4242)
        assert stored is not None
        assert stored.id == thread.id


@respx.mock
async def test_the_card_is_not_repeated_for_a_thread_that_already_has_its_listing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A match's own thread was opened by its card; showing it again is noise.
    listing_id = await _seed_card(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1)]})
    send = respx.post(f"{API}/sendMessage").respond(200, json={"ok": True})

    agent = _FakeAgent(acted_on=listing_id)
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    assert [json.loads(call.request.read())["text"] for call in send.calls] == ["Antwort."]


@respx.mock
async def test_a_card_telegram_refuses_leaves_no_half_written_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    listing_id = await _seed_listing(session_factory)
    respx.post(f"{API}/getUpdates").respond(200, json={"result": [_update(1, thread_id=4321)]})
    respx.post(f"{API}/sendMessage").respond(400, json={"ok": False})

    agent = _FakeAgent(acted_on=listing_id)
    async with httpx.AsyncClient() as client:
        await _poll(_bot(session_factory, agent), client)

    async with session_factory() as session:
        thread = await Repository(session).get_thread_by_thread_id(4321)
        assert thread is not None
        assert thread.listing_id == listing_id
        assert thread.channel_message_id is None  # nothing was posted
