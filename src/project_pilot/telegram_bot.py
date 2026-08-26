"""The bot process: reads the match topics and answers in them.

Long polling, not a webhook: the worker keeps no inbound port, exactly as the
notification side does. ``getUpdates`` blocks on Telegram's side until something
arrives or the timeout expires, so an idle bot costs one open connection and
nothing else.

Routing is the whole trick. Telegram hands every message its
``message_thread_id``; that id maps to a listing in ``telegram_threads``, which
makes the topic the conversation's identity. A message from anywhere else — the
group's general area, a topic a human opened — has no listing and is answered
with one sentence rather than guessed at.
"""

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.agent import AgentReply, ThreadAgent
from project_pilot.db import session_scope
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram holds the request open this long when nothing is happening.
POLL_TIMEOUT_S = 50
# Comfortably under Telegram's own 4096-character message limit.
CHUNK_CHARS = 3_500
# Turns kept per topic; the agent bounds what it sends, this bounds what is stored.
HISTORY_KEEP = 40
_HTTP_TIMEOUT = POLL_TIMEOUT_S + 15

NO_THREAD = (
    "Ich antworte nur in den Projekt-Threads, die project-pilot selbst anlegt. "
    "Öffne den Thread des Projekts und frag dort."
)


@dataclass(frozen=True, slots=True)
class Incoming:
    """One message worth acting on, already narrowed to what routing needs."""

    update_id: int
    chat_id: int
    thread_id: int | None
    user_id: int
    text: str


def parse_updates(payload: Mapping[str, object]) -> list[Incoming]:
    """Pick the plain text messages out of a getUpdates response.

    Everything else — edits, joins, service messages about the topics
    themselves — is skipped here rather than deeper in, so the routing code only
    ever sees messages a human typed.
    """
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    messages = []
    for update in result:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        message = update.get("message")
        if not isinstance(update_id, int) or not isinstance(message, dict):
            continue
        text = message.get("text")
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            continue
        chat_id, user_id = chat.get("id"), sender.get("id")
        thread_id = message.get("message_thread_id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            continue
        messages.append(
            Incoming(
                update_id=update_id,
                chat_id=chat_id,
                thread_id=thread_id if isinstance(thread_id, int) else None,
                user_id=user_id,
                text=text.strip(),
            )
        )
    return messages


def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split a long answer on line breaks where possible, hard-split otherwise.

    Telegram rejects a message past its limit outright, which would lose the
    whole answer rather than shorten it.
    """
    if len(text) <= size:
        return [text]
    parts: list[str] = []
    rest = text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        if cut <= 0:
            cut = size
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest:
        parts.append(rest)
    return parts


class TelegramBot:
    """Polls for messages in the match topics and answers them through the agent."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        allowed_user_ids: Sequence[int],
        agent: ThreadAgent,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._api = f"{API_BASE}/bot{bot_token}"
        self._chat_id = str(chat_id)
        self._allowed = set(allowed_user_ids)
        self._agent = agent
        self._session_factory = session_factory
        self._offset = 0
        # One lock per topic: two quick messages in the same thread are answered
        # in order instead of racing each other's history writes.
        self._locks: dict[int, asyncio.Lock] = {}

    async def run_forever(self) -> None:
        """Poll until cancelled, surviving transient Telegram failures."""
        logger.info("telegram bot started; allowed users: %s", sorted(self._allowed))
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            while True:
                try:
                    await self.poll_once(client)
                except httpx.HTTPError as err:
                    logger.warning("polling failed, retrying: %s", err)
                    await asyncio.sleep(5)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One getUpdates round; returns how many messages were handled."""
        response = await client.post(
            f"{self._api}/getUpdates",
            json={"offset": self._offset, "timeout": POLL_TIMEOUT_S},
        )
        response.raise_for_status()
        payload = response.json()
        incoming = parse_updates(payload if isinstance(payload, dict) else {})
        handled = 0
        for message in incoming:
            # Advance past every update, including the ones that are dropped:
            # an update left unacknowledged is redelivered forever.
            self._offset = max(self._offset, message.update_id + 1)
            if await self._handle(client, message):
                handled += 1
        return handled

    async def _handle(self, client: httpx.AsyncClient, message: Incoming) -> bool:
        if self._allowed and message.user_id not in self._allowed:
            # Anyone else's message is dropped without a reply: answering would
            # confirm the bot is here and burn tokens on a stranger.
            logger.warning("ignoring message from user %s", message.user_id)
            return False
        if str(message.chat_id) != self._chat_id:
            logger.warning("ignoring message from chat %s", message.chat_id)
            return False
        if message.thread_id is None:
            await self._send(client, NO_THREAD, thread_id=None)
            return False

        lock = self._locks.setdefault(message.thread_id, asyncio.Lock())
        async with lock:
            reply = await self._answer(client, message)
        if reply is None:
            return False
        for part in chunk(reply.text):
            await self._send(client, part, thread_id=message.thread_id)
        return True

    async def _answer(self, client: httpx.AsyncClient, message: Incoming) -> AgentReply | None:
        """Route the message to its listing, ask the agent, record the turn."""
        assert message.thread_id is not None
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            thread = await repo.get_thread_by_thread_id(message.thread_id)
            if thread is None:
                await self._send(client, NO_THREAD, thread_id=message.thread_id)
                return None
            listing_id, history = thread.listing_id, list(thread.history)

            # Sent before the model call, which can take a while: silence would
            # read as "the bot ignored me".
            await self._typing(client, message.thread_id)
            reply = await self._agent.reply(
                listing_id=listing_id, history=history, message=message.text
            )
            if reply.ok:
                # A failed turn is not worth remembering; the next message
                # should start from the last state that actually made sense.
                await repo.append_history(
                    thread,
                    [
                        {"role": "user", "text": message.text},
                        {"role": "assistant", "text": reply.text},
                    ],
                    keep=HISTORY_KEEP,
                )
                await session.commit()
            return reply

    async def _typing(self, client: httpx.AsyncClient, thread_id: int) -> None:
        try:
            await client.post(
                f"{self._api}/sendChatAction",
                json={
                    "chat_id": self._chat_id,
                    "message_thread_id": thread_id,
                    "action": "typing",
                },
            )
        except httpx.HTTPError as err:  # a missing typing indicator is cosmetic
            logger.debug("typing action failed: %s", err)

    async def _send(self, client: httpx.AsyncClient, text: str, *, thread_id: int | None) -> None:
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        try:
            response = await client.post(f"{self._api}/sendMessage", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("reply failed in thread %s: %s", thread_id, err)
