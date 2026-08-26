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

Answering runs as its own task rather than inside the poll loop, because a run
can stop mid-way to ask permission for a tool: the question is a message with
two buttons, and the press that answers it arrives through the very same
``getUpdates`` call. Blocking the loop on the answer would deadlock on the
question it just asked.
"""

import asyncio
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.agent import AgentReply, Approve, ThreadAgent
from project_pilot.db import session_scope
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram holds the request open this long when nothing is happening.
POLL_TIMEOUT_S = 50
# Comfortably under Telegram's own 4096-character message limit.
CHUNK_CHARS = 3_500
_HTTP_TIMEOUT = POLL_TIMEOUT_S + 15

NO_THREAD = (
    "Ich antworte nur in den Projekt-Threads, die project-pilot selbst anlegt. "
    "Öffne den Thread des Projekts und frag dort."
)
ALLOW, DENY = "allow", "deny"

# The `/` menu Telegram shows in the chat. These are not handled here — they
# reach the agent as the text they are, and the system prompt says what each
# one means. Registering them buys two things: the menu itself, and a way in
# that works even with the bot's privacy mode still on, since a command is
# always delivered to a bot.
COMMANDS: tuple[tuple[str, str], ...] = (
    ("pruefen", "Projekt gegen das Profil prüfen"),
    ("bewerbung", "Bewerbung für dieses Projekt entwerfen"),
    ("kontakt", "Kontaktdaten der Firma suchen"),
    ("senden", "Bewerbung abschicken (fragt vorher)"),
    ("stand", "Wo steht dieses Projekt gerade?"),
)
# Unanswered questions must not pile up open turns forever.
APPROVAL_TIMEOUT_S = 600


@dataclass(frozen=True, slots=True)
class Press:
    """One button press on a permission question."""

    update_id: int
    callback_id: str
    user_id: int
    decision: str
    request_id: str


def parse_callbacks(payload: Mapping[str, object]) -> list[Press]:
    """Pick the permission answers out of a getUpdates response."""
    result = payload.get("result")
    if not isinstance(result, list):
        return []
    presses = []
    for update in result:
        if not isinstance(update, dict):
            continue
        update_id = update.get("update_id")
        callback = update.get("callback_query")
        if not isinstance(update_id, int) or not isinstance(callback, dict):
            continue
        callback_id, data = callback.get("id"), callback.get("data")
        sender = callback.get("from")
        if not isinstance(callback_id, str) or not isinstance(data, str):
            continue
        if not isinstance(sender, dict) or not isinstance(sender.get("id"), int):
            continue
        decision, _, request_id = data.partition(":")
        if decision not in (ALLOW, DENY) or not request_id:
            continue
        presses.append(
            Press(
                update_id=update_id,
                callback_id=callback_id,
                user_id=int(sender["id"]),
                decision=decision,
                request_id=request_id,
            )
        )
    return presses


def question_text(tool: str, detail: str) -> str:
    """What the permission question says, kept to what fits on a phone."""
    head = f"🔐 Freigabe: {tool}"
    return f"{head}\n{detail}" if detail else head


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
        # in order instead of racing each other's session writes.
        self._locks: dict[int, asyncio.Lock] = {}
        # Answers in flight, and the permission questions they are waiting on.
        self._answering: set[asyncio.Task[None]] = set()
        self._pending: dict[str, asyncio.Future[bool]] = {}

    async def run_forever(self) -> None:
        """Poll until cancelled, surviving transient Telegram failures."""
        logger.info("telegram bot started; allowed users: %s", sorted(self._allowed))
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            await self.register_commands(client)
            while True:
                try:
                    await self.poll_once(client)
                except httpx.HTTPError as err:
                    logger.warning("polling failed, retrying: %s", err)
                    await asyncio.sleep(5)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        """One getUpdates round; returns how many updates were taken up.

        Messages are dispatched, not awaited: an answer may stop to ask for a
        permission whose button press only arrives through a later round.
        """
        response = await client.post(
            f"{self._api}/getUpdates",
            json={"offset": self._offset, "timeout": POLL_TIMEOUT_S},
        )
        response.raise_for_status()
        payload = response.json() if isinstance(response.json(), dict) else {}
        taken = 0
        for press in parse_callbacks(payload):
            # Advance past every update, including the ones that are dropped:
            # an update left unacknowledged is redelivered forever.
            self._offset = max(self._offset, press.update_id + 1)
            if await self._decide(client, press):
                taken += 1
        for message in parse_updates(payload):
            self._offset = max(self._offset, message.update_id + 1)
            if not self._accepts(message):
                continue
            task = asyncio.create_task(self._answer_task(client, message))
            self._answering.add(task)
            task.add_done_callback(self._answering.discard)
            taken += 1
        return taken

    async def register_commands(self, client: httpx.AsyncClient) -> bool:
        """Publish the `/` menu; False if Telegram refused it.

        Best effort on purpose: a missing menu is a worse chat, not a broken
        bot, and the agent understands the same words typed out anyway.
        """
        try:
            response = await client.post(
                f"{self._api}/setMyCommands",
                json={
                    "commands": [
                        {"command": name, "description": description}
                        for name, description in COMMANDS
                    ]
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("could not register the command menu: %s", err)
            return False
        return True

    async def drain(self) -> None:
        """Wait for the answers currently in flight (tests, shutdown)."""
        while self._answering:
            await asyncio.gather(*tuple(self._answering), return_exceptions=True)

    def _accepts(self, message: Incoming) -> bool:
        if self._allowed and message.user_id not in self._allowed:
            # Anyone else's message is dropped without a reply: answering would
            # confirm the bot is here and burn tokens on a stranger.
            logger.warning("ignoring message from user %s", message.user_id)
            return False
        if str(message.chat_id) != self._chat_id:
            logger.warning("ignoring message from chat %s", message.chat_id)
            return False
        return True

    async def _answer_task(self, client: httpx.AsyncClient, message: Incoming) -> None:
        try:
            await self._handle(client, message)
        except Exception:  # a crashed answer must not take the poll loop with it
            logger.exception("answering failed in thread %s", message.thread_id)

    async def _decide(self, client: httpx.AsyncClient, press: Press) -> bool:
        """Hand a button press to the question that is waiting for it."""
        if self._allowed and press.user_id not in self._allowed:
            # Checked before the question is taken off the pile: a stranger's
            # press must not consume the answer the owner is still going to give.
            logger.warning("ignoring permission press from user %s", press.user_id)
            return False
        waiting = self._pending.pop(press.request_id, None)
        allowed = press.decision == ALLOW
        await self._answer_callback(
            client, press.callback_id, "Erlaubt" if allowed else "Abgelehnt"
        )
        if waiting is None or waiting.done():
            return False
        waiting.set_result(allowed)
        return True

    async def _handle(self, client: httpx.AsyncClient, message: Incoming) -> bool:
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
            listing_id, session_id = thread.listing_id, thread.session_id

            # Sent before the model call, which can take a while: silence would
            # read as "the bot ignored me".
            await self._typing(client, message.thread_id)
            reply = await self._agent.reply(
                listing_id=listing_id,
                session_id=session_id,
                message=message.text,
                approve=self._approver(client, message.thread_id),
            )
            if reply.session_id and reply.session_id != session_id:
                # Stored even when the turn failed: the session exists either
                # way, and losing its id would restart the topic from nothing.
                await repo.set_session_id(thread, reply.session_id)
                await session.commit()
            return reply

    def _approver(self, client: httpx.AsyncClient, thread_id: int) -> Approve:
        """A permission question bound to one topic, as the agent expects it."""

        async def approve(tool: str, detail: str) -> bool:
            return await self._ask(client, thread_id, tool, detail)

        return approve

    async def _ask(self, client: httpx.AsyncClient, thread_id: int, tool: str, detail: str) -> bool:
        """Put the question in the thread and wait for the button.

        Refuses on timeout and on a failed send: an unanswered question must not
        hold a turn open, and silently allowing would defeat the point of asking.
        """
        request_id = secrets.token_urlsafe(8)
        waiting: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiting
        message_id = await self._send(
            client,
            question_text(tool, detail),
            thread_id=thread_id,
            keyboard={
                "inline_keyboard": [
                    [
                        {"text": "✅ Erlauben", "callback_data": f"{ALLOW}:{request_id}"},
                        {"text": "🚫 Ablehnen", "callback_data": f"{DENY}:{request_id}"},
                    ]
                ]
            },
        )
        if message_id is None:
            self._pending.pop(request_id, None)
            return False
        try:
            allowed = await asyncio.wait_for(waiting, timeout=APPROVAL_TIMEOUT_S)
        except TimeoutError:
            allowed = False
            logger.warning("permission question %s timed out (%s)", request_id, tool)
        finally:
            self._pending.pop(request_id, None)
        # Rewrite the question into its answer, so the thread reads as a record
        # rather than leaving live buttons on a decision already made.
        await self._edit(
            client,
            message_id,
            f"{question_text(tool, detail)}\n\n{'✅ erlaubt' if allowed else '🚫 abgelehnt'}",
        )
        return allowed

    async def _edit(self, client: httpx.AsyncClient, message_id: int, text: str) -> None:
        try:
            await client.post(
                f"{self._api}/editMessageText",
                json={"chat_id": self._chat_id, "message_id": message_id, "text": text},
            )
        except httpx.HTTPError as err:  # cosmetic; the decision already stands
            logger.debug("editing question %s failed: %s", message_id, err)

    async def _answer_callback(
        self, client: httpx.AsyncClient, callback_id: str, text: str
    ) -> None:
        try:
            await client.post(
                f"{self._api}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
            )
        except httpx.HTTPError as err:  # only the little toast in the client
            logger.debug("answering callback failed: %s", err)

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

    async def _send(
        self,
        client: httpx.AsyncClient,
        text: str,
        *,
        thread_id: int | None,
        keyboard: dict[str, object] | None = None,
    ) -> int | None:
        """Send one message; returns its id, or None if Telegram refused it."""
        payload: dict[str, object] = {
            "chat_id": self._chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if keyboard is not None:
            payload["reply_markup"] = keyboard
        try:
            response = await client.post(f"{self._api}/sendMessage", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("reply failed in thread %s: %s", thread_id, err)
            return None
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) else None
