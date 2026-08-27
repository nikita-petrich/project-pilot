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
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from project_pilot.agent import AgentReply, Approve, Progress, ThreadAgent
from project_pilot.db import session_scope
from project_pilot.mcp_prompts import PROMPTS, render
from project_pilot.notification.messages import from_stored
from project_pilot.notification.telegram import match_keyboard, match_text
from project_pilot.repository import Repository

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
# Telegram holds the request open this long when nothing is happening.
POLL_TIMEOUT_S = 50
# Comfortably under Telegram's own 4096-character message limit.
CHUNK_CHARS = 3_500
_HTTP_TIMEOUT = POLL_TIMEOUT_S + 15

# Telegram omits message_thread_id in a group's General area; 0 gives that
# conversation an identity of its own so it can hold a session like any topic.
GENERAL = 0
# What a `/command` fills its slot with when the thread has no listing yet.
OPEN_SLOT = "die Ausschreibung, die Nik hier schickt"
# A topic opened for a message from the group's main area. Telegram caps the
# name at 128; well short of it keeps the sidebar readable.
TOPIC_NAME_CHARS = 60
UNNAMED_TOPIC = "Neue Anfrage"
# The green Telegram offers; a thread you started should not look like a match.
ICON_COLOR_GREEN = 9367192
OPENED = "💬 Zum Thread"
ALLOW, DENY = "allow", "deny"
ACCEPT, DECLINE, DESCRIBE = "accept", "decline", "describe"
CARD_ACTIONS = (ACCEPT, DECLINE, DESCRIBE)

# The `/` menu Telegram shows in the chat: the MCP workflow prompts, by their
# own names and descriptions. Nothing is defined here — a command is expanded
# into the very prompt body the MCP server serves, so the bot and every other
# surface run the same procedure.
COMMANDS: tuple[tuple[str, str], ...] = tuple(
    (name, description) for name, (description, _body) in PROMPTS.items()
)
NO_DESCRIPTION = "Zu diesem Projekt ist keine Beschreibung gespeichert."
DECLINED = "🚫 Abgelehnt."
# Unanswered questions must not pile up open turns forever.
APPROVAL_TIMEOUT_S = 600

# Telegram drops a chat action after about five seconds, so a turn that runs for
# minutes needs it renewed to stay visible.
TYPING_EVERY_S = 4.0
# A reaction on the message being worked on: seen, and done. Best effort — the
# allowed set is Telegram's, and a refused reaction is cosmetic.
SEEN, DONE = "👀", "👍"


@dataclass(frozen=True, slots=True)
class Press:
    """One button press: a permission answer, or a decision on a match card."""

    update_id: int
    callback_id: str
    user_id: int
    action: str
    argument: str
    thread_id: int | None = None
    message_id: int | None = None


def parse_callbacks(payload: Mapping[str, object]) -> list[Press]:
    """Pick the button presses out of a getUpdates response."""
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
        action, _, argument = data.partition(":")
        if action not in (ALLOW, DENY, *CARD_ACTIONS) or not argument:
            continue
        origin = callback.get("message")
        origin = origin if isinstance(origin, dict) else {}
        thread_id = origin.get("message_thread_id")
        message_id = origin.get("message_id")
        presses.append(
            Press(
                update_id=update_id,
                callback_id=callback_id,
                user_id=int(sender["id"]),
                action=action,
                argument=argument,
                thread_id=thread_id if isinstance(thread_id, int) else None,
                message_id=message_id if isinstance(message_id, int) else None,
            )
        )
    return presses


def topic_name(text: str) -> str:
    """A thread name from what was written, without the command that started it."""
    words = text.split()
    if words and words[0].startswith("/"):
        words = words[1:]
    flat = " ".join(words)
    if not flat:
        return UNNAMED_TOPIC
    return flat if len(flat) <= TOPIC_NAME_CHARS else flat[:TOPIC_NAME_CHARS].rstrip() + " …"


def _key(thread_id: int | None) -> int:
    """The identity a conversation is stored under; General has none of its own."""
    return GENERAL if thread_id is None else thread_id


def expand_command(text: str) -> str | None:
    """Turn `/write_application` into the workflow it stands for, else None.

    Telegram sends `/name` and, in a group, `/name@thebot`; anything after that
    is the user's own addition and is kept.
    """
    if not text.startswith("/"):
        return None
    head, _, rest = text[1:].partition(" ")
    name = head.split("@", 1)[0]
    if name not in PROMPTS:
        return None
    body = render(name, "{listing}")
    return f"{body}\n\n{rest.strip()}" if rest.strip() else body


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
    # The message itself, so the answer can react on it rather than only
    # eventually appearing somewhere below it.
    message_id: int | None = None


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
        message_id = message.get("message_id")
        messages.append(
            Incoming(
                update_id=update_id,
                chat_id=chat_id,
                thread_id=thread_id if isinstance(thread_id, int) else None,
                user_id=user_id,
                text=text.strip(),
                message_id=message_id if isinstance(message_id, int) else None,
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
                except Exception as err:
                    # Any exception, not only HTTP: a poller that dies on one
                    # malformed update or one unforeseen bug goes silent, and a
                    # silent bot is indistinguishable from a bot that ignores
                    # you. Log it and keep polling.
                    logger.exception("polling failed, retrying: %s", err)
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
        body = response.json()
        payload = body if isinstance(body, dict) else {}
        results = payload.get("result")
        logger.info("received %d update(s)", len(results) if isinstance(results, list) else 0)
        taken = 0
        for press in parse_callbacks(payload):
            # Advance past every update, including the ones that are dropped:
            # an update left unacknowledged is redelivered forever.
            self._offset = max(self._offset, press.update_id + 1)
            try:
                if await self._press(client, press):
                    taken += 1
            except Exception:  # one bad press must not stop the round
                logger.exception("handling press %s failed", press.action)
        for message in parse_updates(payload):
            self._offset = max(self._offset, message.update_id + 1)
            if not self._accepts(message):
                continue
            logger.info("answering message %s in thread %s", message.update_id, message.thread_id)
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

    async def _press(self, client: httpx.AsyncClient, press: Press) -> bool:
        """Route one button press: a permission answer, or a card decision."""
        if self._allowed and press.user_id not in self._allowed:
            # Checked before anything is taken off a pile: a stranger's press
            # must not consume the answer the owner is still going to give.
            logger.warning("ignoring press from user %s", press.user_id)
            return False
        if press.action in CARD_ACTIONS:
            return await self._card(client, press)
        return await self._decide(client, press)

    async def _decide(self, client: httpx.AsyncClient, press: Press) -> bool:
        """Hand a permission answer to the question that is waiting for it."""
        waiting = self._pending.pop(press.argument, None)
        allowed = press.action == ALLOW
        await self._answer_callback(
            client, press.callback_id, "Erlaubt" if allowed else "Abgelehnt"
        )
        if waiting is None or waiting.done():
            return False
        waiting.set_result(allowed)
        return True

    async def _card(self, client: httpx.AsyncClient, press: Press) -> bool:
        """Act on one of the three decisions a match card offers."""
        try:
            listing_id = int(press.argument)
        except ValueError:
            logger.warning("card press with a bad listing id: %r", press.argument)
            return False
        if press.action == DESCRIBE:
            await self._answer_callback(client, press.callback_id, "Beschreibung")
            await self._describe(client, listing_id, thread_id=press.thread_id)
            return True
        if press.action == DECLINE:
            await self._answer_callback(client, press.callback_id, "Abgelehnt")
            await self._decline(client, press)
            return True
        await self._answer_callback(client, press.callback_id, "Angenommen")
        # Accepting is real work, so it goes the same way a message does: its
        # own task, because the run may stop to ask for a permission whose
        # press can only arrive through a later poll.
        task = asyncio.create_task(self._accept(client, listing_id, press))
        self._answering.add(task)
        task.add_done_callback(self._answering.discard)
        return True

    async def _describe(
        self, client: httpx.AsyncClient, listing_id: int, *, thread_id: int | None
    ) -> None:
        """Post the listing's own text, which the card deliberately leaves out."""
        async with session_scope(self._session_factory) as session:
            listing = await Repository(session).get_listing(listing_id)
        text = (listing.description or "").strip() if listing else ""
        for part in chunk(text or NO_DESCRIPTION):
            await self._send(client, part, thread_id=thread_id)

    async def _decline(self, client: httpx.AsyncClient, press: Press) -> None:
        """Close the topic and take the buttons off the card.

        Closed, not deleted: the record of what was offered and turned down is
        worth more than a tidy list, and Telegram can reopen a closed topic.
        """
        if press.message_id is not None:
            await self._clear_keyboard(client, press.message_id)
            await self._send(client, DECLINED, thread_id=press.thread_id)
        if press.thread_id is not None:
            await self._close_topic(client, press.thread_id)

    async def _accept(self, client: httpx.AsyncClient, listing_id: int, press: Press) -> None:
        """Start the work: run the drafting workflow where the card is.

        Usually a match's own topic; a card from ``test-match`` sits in the
        group's General area, which holds a conversation just the same.
        """
        if press.message_id is not None:
            await self._clear_keyboard(client, press.message_id)
        await self._run(
            client,
            thread_id=press.thread_id,
            listing_id=listing_id,
            message=render("write_application", f"Listing {listing_id}"),
        )

    async def _handle(self, client: httpx.AsyncClient, message: Incoming) -> bool:
        """Answer wherever the message came from, opening the topic's session.

        A topic project-pilot created is about its match; one you opened
        yourself is about whatever you bring into it, and gets its own session
        the first time you write there.
        """
        thread_id = message.thread_id
        if thread_id is None:
            # Anything written in the group's main area gets a thread of its own,
            # so the answer, its steps and every follow-up stay together instead
            # of interleaving with the next thing you drop there.
            name = topic_name(message.text)
            thread_id = await self._open_topic(client, name)
            if thread_id is not None:
                # A link back, because the new topic is only visible in the
                # sidebar otherwise and the main area shows no trace of it.
                await self._send(
                    client,
                    f"→ {name}",
                    thread_id=None,
                    keyboard=self._topic_link(thread_id),
                )
                await self._send(client, message.text, thread_id=thread_id)
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            thread = await repo.ensure_thread(_key(thread_id))
            listing_id = thread.listing_id
            await session.commit()
        # A `/command` is not handled here: it stands for one of the MCP
        # workflow prompts, and what reaches the agent is that prompt's own
        # body, so the bot runs the same procedure every other surface runs.
        text = expand_command(message.text) or message.text
        slot = f"Listing {listing_id}" if listing_id is not None else OPEN_SLOT
        text = text.replace("{listing}", slot)
        return await self._run(
            client,
            thread_id=thread_id,
            listing_id=listing_id,
            message=text,
            react_to=message.message_id,
        )

    async def _run(
        self,
        client: httpx.AsyncClient,
        *,
        thread_id: int | None,
        listing_id: int | None,
        message: str,
        react_to: int | None = None,
    ) -> bool:
        """One agent turn in one topic, serialized per topic, answered there.

        Everything around the turn exists so it never looks stalled: the message
        is marked seen, the typing indicator is renewed, and one status line
        names the step the agent is on.
        """
        lock = self._locks.setdefault(_key(thread_id), asyncio.Lock())
        async with lock:
            if react_to is not None:
                await self._react(client, react_to, SEEN)
            typing = asyncio.create_task(self._keep_typing(client, thread_id))
            report, status = self._progress(client, thread_id)
            try:
                reply = await self._answer(
                    client,
                    thread_id=thread_id,
                    listing_id=listing_id,
                    message=message,
                    progress=report,
                )
            finally:
                typing.cancel()
                for message_id in status:
                    await self._delete(client, message_id)
        if react_to is not None:
            # Cleared rather than marked done when the turn failed: a thumb up
            # over an error message would be a lie.
            await self._react(client, react_to, DONE if reply.ok else None)
        for part in chunk(reply.text):
            await self._send(client, part, thread_id=thread_id)
        return True

    async def _answer(
        self,
        client: httpx.AsyncClient,
        *,
        thread_id: int | None,
        listing_id: int | None,
        message: str,
        progress: Progress,
    ) -> AgentReply:
        """Ask the agent, and record the session the topic continues in."""
        async with session_scope(self._session_factory) as session:
            repo = Repository(session)
            thread = await repo.ensure_thread(_key(thread_id))
            session_id = thread.session_id

            reply = await self._agent.reply(
                listing_id=listing_id,
                session_id=session_id,
                message=message,
                approve=self._approver(client, thread_id),
                progress=progress,
            )
            if reply.session_id and reply.session_id != session_id:
                # Stored even when the turn failed: the session exists either
                # way, and losing its id would restart the topic from nothing.
                await repo.set_session_id(thread, reply.session_id)
            bound = (
                reply.listing_id is not None
                and listing_id is None
                and await repo.set_listing_id(thread, reply.listing_id)
            )
            await session.commit()
            if bound and reply.listing_id is not None:
                await self._show_card(client, repo, reply.listing_id, thread_id=thread_id)
            return reply

    async def _show_card(
        self,
        client: httpx.AsyncClient,
        repo: Repository,
        listing_id: int,
        *,
        thread_id: int | None,
    ) -> None:
        """Show the match card for a listing the agent just took on.

        The same card a scan match gets, buttons and all, built from the stored
        verdict rather than by asking the model to format one.
        """
        listing = await repo.get_listing_with_evaluations(listing_id)
        if listing is None:
            return
        message = from_stored(listing, datetime.now(UTC))
        await self._send(
            client, match_text(message), thread_id=thread_id, keyboard=match_keyboard(message)
        )

    def _approver(self, client: httpx.AsyncClient, thread_id: int | None) -> Approve:
        """A permission question bound to one topic, as the agent expects it."""

        async def approve(tool: str, detail: str) -> bool:
            return await self._ask(client, thread_id, tool, detail)

        return approve

    async def _ask(
        self, client: httpx.AsyncClient, thread_id: int | None, tool: str, detail: str
    ) -> bool:
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

    async def _react(self, client: httpx.AsyncClient, message_id: int, emoji: str | None) -> None:
        """Put one reaction on a message, or clear it with an empty list."""
        reaction = [] if emoji is None else [{"type": "emoji", "emoji": emoji}]
        try:
            await client.post(
                f"{self._api}/setMessageReaction",
                json={
                    "chat_id": self._chat_id,
                    "message_id": message_id,
                    "reaction": reaction,
                },
            )
        except httpx.HTTPError as err:  # decoration, never the point
            logger.debug("reaction on %s failed: %s", message_id, err)

    async def _keep_typing(self, client: httpx.AsyncClient, thread_id: int | None) -> None:
        """Hold the typing indicator up for as long as the turn runs."""
        while True:
            await self._typing(client, thread_id)
            await asyncio.sleep(TYPING_EVERY_S)

    def _progress(
        self, client: httpx.AsyncClient, thread_id: int | None
    ) -> tuple[Progress, list[int]]:
        """A single line in the thread saying what the agent is doing right now.

        Sent on the first step and edited afterwards, so a long turn reports
        one moving line rather than a wall of status messages. The returned
        list holds its message id, for the caller to clean up.
        """
        holder: list[int] = []
        seen: list[str] = []

        async def report(label: str) -> None:
            if seen and seen[-1] == label:
                return  # the same tool twice in a row is not news
            seen.append(label)
            text = f"⏳ {label} …"
            if holder:
                await self._edit(client, holder[0], text)
                return
            message_id = await self._send(client, text, thread_id=thread_id)
            if message_id is not None:
                holder.append(message_id)

        return report, holder

    async def _delete(self, client: httpx.AsyncClient, message_id: int) -> None:
        try:
            await client.post(
                f"{self._api}/deleteMessage",
                json={"chat_id": self._chat_id, "message_id": message_id},
            )
        except httpx.HTTPError as err:  # a leftover status line is not a failure
            logger.debug("deleting %s failed: %s", message_id, err)

    async def _clear_keyboard(self, client: httpx.AsyncClient, message_id: int) -> None:
        """Take the buttons off a card that has been decided."""
        try:
            await client.post(
                f"{self._api}/editMessageReplyMarkup",
                json={"chat_id": self._chat_id, "message_id": message_id},
            )
        except httpx.HTTPError as err:  # cosmetic; the decision already stands
            logger.debug("clearing the keyboard on %s failed: %s", message_id, err)

    def _topic_link(self, thread_id: int) -> dict[str, object] | None:
        """A button opening one topic, for chats whose id can address one.

        Only a supergroup has the ``t.me/c/<id>/<thread>`` form; anything else
        gets no button rather than a link that goes nowhere.
        """
        if not self._chat_id.startswith("-100"):
            return None
        internal = self._chat_id.removeprefix("-100")
        return {
            "inline_keyboard": [[{"text": OPENED, "url": f"https://t.me/c/{internal}/{thread_id}"}]]
        }

    async def _open_topic(self, client: httpx.AsyncClient, name: str) -> int | None:
        """Open a thread for a message from the main area; None if it cannot.

        A chat that is not a forum, or a bot without Manage Topics, must fall
        back to answering where the message was rather than dropping it.
        """
        try:
            response = await client.post(
                f"{self._api}/createForumTopic",
                json={"chat_id": self._chat_id, "name": name, "icon_color": ICON_COLOR_GREEN},
            )
            response.raise_for_status()
        except httpx.HTTPError as err:
            logger.warning("could not open a topic for %r: %s", name, err)
            return None
        body = response.json()
        result = body.get("result") if isinstance(body, dict) else None
        thread_id = result.get("message_thread_id") if isinstance(result, dict) else None
        return thread_id if isinstance(thread_id, int) else None

    async def _close_topic(self, client: httpx.AsyncClient, thread_id: int) -> None:
        try:
            await client.post(
                f"{self._api}/closeForumTopic",
                json={"chat_id": self._chat_id, "message_thread_id": thread_id},
            )
        except httpx.HTTPError as err:  # a topic left open is not a failure
            logger.warning("closing topic %s failed: %s", thread_id, err)

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

    async def _typing(self, client: httpx.AsyncClient, thread_id: int | None) -> None:
        payload: dict[str, object] = {"chat_id": self._chat_id, "action": "typing"}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        try:
            await client.post(f"{self._api}/sendChatAction", json=payload)
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
