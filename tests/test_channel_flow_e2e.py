"""End to end over the real channel flow, with only Telegram and the model faked.

Every other test here checks one link. This one walks the whole chain the way it
actually runs in production, against a real database and the real
``TelegramNotifier``, ``TelegramBot`` and ``Repository``:

1. the worker posts a match card to the **channel**
2. Telegram forwards it into the **discussion group**, which is what opens the
   comment thread and what tells the bot which thread belongs to which listing
3. someone writes in that thread and the agent answers there
4. the description button posts the listing text into the same thread
5. declining deletes the post *and* the thread, and forgets both

The failures this is here to catch are the ones a unit test cannot see: an id
from one chat used in the other, a thread that is never bound because the
forward was parsed as a human message, a row that survives a decline and blocks
the listing from ever being posted again.
"""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import respx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.agent import AgentReply, Approve, Progress, allow_everything, report_nothing
from project_pilot.db import session_scope
from project_pilot.models import Evaluation, EvaluationStage, Listing, Verdict
from project_pilot.notification.messages import from_stored
from project_pilot.notification.telegram import TelegramNotifier
from project_pilot.repository import Repository
from project_pilot.telegram_bot import TelegramBot

BOT_TOKEN = "123456:AAtest"
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
CHANNEL = "-1001111111111"
GROUP = "-1002222222222"
ME = 4242

# What Telegram will hand back, in the order the flow produces it.
POST_ID = 3001  # the card in the channel
ROOT_ID = 4001  # its forwarded copy in the group — the comment thread's root
DESCRIPTION = "Wir suchen einen Senior Python Entwickler für unsere Plattform."
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


class _Agent:
    """Answers whatever it is asked, and records the routing it was given."""

    def __init__(self) -> None:
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
        await progress("prüfe das Listing gegen dein Profil")
        return AgentReply(text="Passt gut zu deinem Profil.", ok=True, session_id="sess-e2e")


class _Telegram:
    """A Telegram stand-in that hands out message ids and records every call."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.deleted: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self._next_id = 9000

    def install(self) -> None:
        respx.post(f"{API}/getChat").respond(
            200,
            json={"ok": True, "result": {"id": int(CHANNEL), "linked_chat_id": int(GROUP)}},
        )
        respx.post(f"{API}/sendMessage").mock(side_effect=self._send)
        respx.post(f"{API}/deleteMessage").mock(side_effect=self._delete)
        respx.post(f"{API}/editMessageText").mock(side_effect=self._edit)
        for method in (
            "answerCallbackQuery",
            "editMessageReplyMarkup",
            "setMessageReaction",
            "sendChatAction",
            "setMyCommands",
        ):
            respx.post(f"{API}/{method}").respond(200, json={"ok": True})

    def _send(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        self.sent.append(payload)
        # The card is the first thing ever posted to the channel; give it the
        # id the forward will later name, so the two halves have to match up.
        if payload["chat_id"] == CHANNEL and len(self.to(CHANNEL)) == 1:
            message_id = POST_ID
        else:
            self._next_id += 1
            message_id = self._next_id
        return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})

    def _delete(self, request: httpx.Request) -> httpx.Response:
        self.deleted.append(json.loads(request.read()))
        return httpx.Response(200, json={"ok": True})

    def _edit(self, request: httpx.Request) -> httpx.Response:
        self.edited.append(json.loads(request.read()))
        return httpx.Response(200, json={"ok": True})

    def to(self, chat_id: str) -> list[dict[str, Any]]:
        return [payload for payload in self.sent if payload["chat_id"] == chat_id]


def _updates(*results: dict[str, Any]) -> None:
    respx.post(f"{API}/getUpdates").respond(200, json={"ok": True, "result": list(results)})


async def _seed_match(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """One evaluated match, exactly as the scan pipeline leaves it."""
    async with session_scope(session_factory) as session:
        repo = Repository(session)
        listing, _ = await repo.upsert_listing(
            Listing(
                source="freelancermap",
                external_url="https://example.test/projekt/1",
                url_hash="e2e-hash",
                title="Senior Python Developer",
                description=DESCRIPTION,
                skills=["Python", "FastAPI"],
                raw={"company": "ACME GmbH"},
            )
        )
        await repo.add_evaluation(
            Evaluation(
                listing_id=listing.id,
                stage=EvaluationStage.LLM,
                verdict=Verdict.MATCH,
                score=87,
                reason={"reasons": ["Stack passt"], "risk_flags": []},
            )
        )
        await session.commit()
        return listing.id


@respx.mock
async def test_a_match_travels_from_the_channel_into_its_thread_and_back_out(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    telegram = _Telegram()
    telegram.install()
    listing_id = await _seed_match(session_factory)
    agent = _Agent()
    bot = TelegramBot(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL,
        allowed_user_ids=[ME],
        agent=agent,  # type: ignore[arg-type]
        session_factory=session_factory,
    )

    async with httpx.AsyncClient() as client:
        # 1. The worker posts the card to the channel and records its id.
        notifier = TelegramNotifier(bot_token=BOT_TOKEN, chat_id=CHANNEL)
        async with session_scope(session_factory) as session:
            repo = Repository(session)
            listing = await repo.get_listing_with_evaluations(listing_id)
            assert listing is not None
            message = from_stored(listing, NOW)
            posted = await notifier.notify(message)
            assert posted == POST_ID
            await repo.record_channel_message(listing_id, posted)
            await repo.mark_notified([listing], NOW)
            await session.commit()

        card = telegram.to(CHANNEL)[0]
        assert "🏢 Company: ACME GmbH" in card["text"]
        assert "🎯 Score: 87/100" in card["text"]
        assert DESCRIPTION not in card["text"]  # it rides behind its own button

        # 2. Telegram forwards the post into the group; that is the thread.
        _updates(
            {
                "update_id": 1,
                "message": {
                    "message_id": ROOT_ID,
                    "chat": {"id": int(GROUP)},
                    "is_automatic_forward": True,
                    "forward_origin": {
                        "type": "channel",
                        "chat": {"id": int(CHANNEL)},
                        "message_id": POST_ID,
                    },
                    "text": card["text"],
                },
            }
        )
        assert await bot.poll_once(client) == 1
        await bot.drain()
        # The forward is not a human message: nothing was answered.
        assert agent.calls == []

        async with session_factory() as session:
            thread = await Repository(session).get_thread_by_thread_id(ROOT_ID)
            assert thread is not None
            assert thread.listing_id == listing_id

        # 3. A comment in that thread is answered there, about that listing.
        _updates(
            {
                "update_id": 2,
                "message": {
                    "message_id": 4002,
                    "chat": {"id": int(GROUP)},
                    "from": {"id": ME},
                    "message_thread_id": ROOT_ID,
                    "text": "wie schätzt du das ein?",
                },
            }
        )
        await bot.poll_once(client)
        await bot.drain()

        assert agent.calls == [(listing_id, None, "wie schätzt du das ein?")]
        answer = telegram.to(GROUP)[-1]
        assert answer["text"] == "Passt gut zu deinem Profil."
        assert answer["reply_parameters"]["message_id"] == ROOT_ID
        # The status line was posted and then taken away again.
        assert any("⏳" in payload["text"] for payload in telegram.to(GROUP))
        assert telegram.deleted and telegram.deleted[-1]["chat_id"] == GROUP

        # 4. The description button posts the listing text into the same thread.
        _updates(
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cb3",
                    "from": {"id": ME},
                    "data": f"describe:{listing_id}",
                    "message": {"message_id": POST_ID, "chat": {"id": int(CHANNEL)}},
                },
            }
        )
        await bot.poll_once(client)
        await bot.drain()

        described = telegram.to(GROUP)[-1]
        assert described["text"] == DESCRIPTION
        assert described["reply_parameters"]["message_id"] == ROOT_ID

        # 5. Declining takes the post and the thread away, and forgets both.
        telegram.deleted.clear()
        _updates(
            {
                "update_id": 4,
                "callback_query": {
                    "id": "cb4",
                    "from": {"id": ME},
                    "data": f"decline:{listing_id}",
                    "message": {"message_id": POST_ID, "chat": {"id": int(CHANNEL)}},
                },
            }
        )
        await bot.poll_once(client)
        await bot.drain()

    assert telegram.deleted == [
        {"chat_id": CHANNEL, "message_id": POST_ID},
        {"chat_id": GROUP, "message_id": ROOT_ID},
    ]
    async with session_factory() as session:
        repo = Repository(session)
        assert await repo.get_thread_by_thread_id(ROOT_ID) is None
        assert await repo.get_thread_by_channel_message(POST_ID) is None
        # The match itself survives: the group is a work surface, not the archive.
        stored = await repo.get_listing(listing_id)
        assert stored is not None
        assert stored.notified_at is not None


@respx.mock
async def test_the_channel_and_the_group_never_borrow_each_others_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The one class of bug this architecture invites, asserted directly.

    Every write is addressed to exactly one of the two chats, and the ids that
    belong to one must never be sent to the other: a card id used as a thread
    root, or a group message deleted out of the channel, both fail silently in
    production and look like "the bot does nothing".
    """
    telegram = _Telegram()
    telegram.install()
    listing_id = await _seed_match(session_factory)
    async with session_scope(session_factory) as session:
        repo = Repository(session)
        thread = await repo.record_channel_message(listing_id, POST_ID)
        await repo.bind_thread_id(thread, ROOT_ID)
        await session.commit()

    bot = TelegramBot(
        bot_token=BOT_TOKEN,
        chat_id=CHANNEL,
        allowed_user_ids=[ME],
        agent=_Agent(),  # type: ignore[arg-type]
        session_factory=session_factory,
    )
    async with httpx.AsyncClient() as client:
        _updates(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb1",
                    "from": {"id": ME},
                    "data": f"describe:{listing_id}",
                    "message": {"message_id": POST_ID, "chat": {"id": int(CHANNEL)}},
                },
            }
        )
        await bot.poll_once(client)
        await bot.drain()

    # Nothing was written into the channel by a thread action, and every reply
    # in the group hangs under the group's own root — never under the post id.
    assert telegram.to(CHANNEL) == []
    for payload in telegram.to(GROUP):
        params = payload.get("reply_parameters")
        if params is not None:
            assert params["message_id"] == ROOT_ID
        # A discussion group has no forum topics; this field would be rejected.
        assert "message_thread_id" not in payload
