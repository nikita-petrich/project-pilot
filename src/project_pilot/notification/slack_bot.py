"""Slack bot: routes Block-Kit button actions, ``/apply``, and thread replies.

The routing is pure and unit-tested; the Socket Mode connection that feeds it is
wired in ``cli.py`` (network boundary). Only the configured channel is served, and
every state change is guarded in the service layer.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from project_pilot.application.service import DraftView, is_email
from project_pilot.errors import ApplicationStateError, EmailSendError, LlmSchemaError
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.notification.slack import (
    Block,
    PostedMessage,
    draft_fallback_text,
    format_draft_blocks,
)

logger = logging.getLogger(__name__)

USAGE = "Nutzung: `/apply <freelancermap-link oder Projektbeschreibung>`"


class SlackPoster(Protocol):
    """The Slack posting surface the bot needs (``SlackClient`` satisfies it)."""

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> PostedMessage | None: ...

    async def post_text(
        self, text: str, *, thread_ts: str | None = None
    ) -> PostedMessage | None: ...

    async def update_blocks(
        self, channel: str, ts: str, blocks: list[Block], text: str
    ) -> bool: ...


class ApplicationFlow(Protocol):
    """The application-service surface the bot drives."""

    async def draft_for_listing(self, listing_id: int) -> DraftView: ...
    async def draft_from_parsed(self, parsed: ParsedListing) -> DraftView: ...
    async def draft_from_text(self, text: str) -> DraftView: ...
    async def revise(self, application_id: int, instruction: str) -> DraftView: ...
    async def set_recipient(self, application_id: int, email: str) -> DraftView: ...
    async def send(self, application_id: int) -> DraftView: ...
    async def cancel(self, application_id: int) -> DraftView: ...
    async def record_draft_ref(self, application_id: int, draft_ref: str) -> None: ...
    async def find_by_draft_ref(self, draft_ref: str) -> DraftView | None: ...
    async def find_listing_id_by_url(self, url: str) -> int | None: ...


type ListingFetcher = Callable[[str], Awaitable[ParsedListing]]


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


class SlackBot:
    """Turns parsed Slack envelopes into application-flow calls."""

    def __init__(
        self,
        *,
        client: SlackPoster,
        channel: str,
        service: ApplicationFlow,
        fetcher: ListingFetcher | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._service = service
        self._fetcher = fetcher

    async def dispatch(self, envelope_type: str, payload: dict[str, object]) -> None:
        """Parse a Socket Mode envelope and route it (defensive: never raises on shape)."""
        if envelope_type == "interactive":
            await self._dispatch_interactive(payload)
        elif envelope_type == "events_api":
            await self._dispatch_event(payload)
        elif envelope_type == "slash_commands":
            await self._dispatch_command(payload)

    async def _dispatch_interactive(self, payload: dict[str, object]) -> None:
        if payload.get("type") != "block_actions":
            return
        channel = _text(_mapping(payload.get("channel")).get("id"))
        message_ts = _text(_mapping(payload.get("message")).get("ts"))
        actions = payload.get("actions")
        if channel is None or message_ts is None or not isinstance(actions, list):
            return
        for action in actions:
            action_map = _mapping(action)
            action_id = _text(action_map.get("action_id"))
            value = _text(action_map.get("value"))
            if action_id is not None:
                await self.on_block_action(action_id, value, channel, message_ts)

    async def _dispatch_event(self, payload: dict[str, object]) -> None:
        event = _mapping(payload.get("event"))
        if event.get("type") != "message":
            return
        from_bot = bool(event.get("bot_id")) or event.get("subtype") is not None
        await self.on_thread_message(
            channel=_text(event.get("channel")),
            thread_ts=_text(event.get("thread_ts")),
            text=_text(event.get("text")) or "",
            from_bot=from_bot,
        )

    async def _dispatch_command(self, payload: dict[str, object]) -> None:
        if _text(payload.get("command")) != "/apply":
            return
        await self.on_slash_apply(
            channel_id=_text(payload.get("channel_id")),
            text=_text(payload.get("text")) or "",
        )

    async def on_block_action(
        self, action_id: str, value: str | None, channel: str, message_ts: str
    ) -> None:
        if channel != self._channel:
            return
        if action_id == "apply" and value is not None and value.isdigit():
            await self._post_new_draft(lambda: self._service.draft_for_listing(int(value)))
        elif action_id == "send" and value is not None and value.isdigit():
            await self._send_application(int(value), message_ts)
        elif action_id == "cancel" and value is not None and value.isdigit():
            await self._update_from(message_ts, lambda: self._service.cancel(int(value)))
        # open_mail / open_project are URL buttons handled by Slack itself.

    async def on_slash_apply(self, channel_id: str | None, text: str) -> None:
        argument = text.strip()
        if not argument:
            await self._client.post_text(USAGE)
            return
        if not argument.lower().startswith(("http://", "https://")):
            await self._post_new_draft(lambda: self._service.draft_from_text(argument))
            return
        listing_id = await self._service.find_listing_id_by_url(argument)
        if listing_id is not None:
            await self._post_new_draft(lambda: self._service.draft_for_listing(listing_id))
            return
        if "freelancermap." not in argument or self._fetcher is None:
            await self._client.post_text(
                "⚠️ Diesen Link kenne ich nicht. Nutze `/apply` mit der "
                "Projektbeschreibung als Text."
            )
            return
        fetcher = self._fetcher
        await self._client.post_text("⏳ Lade das Projekt …")

        async def fetch_and_draft() -> DraftView:
            parsed = await fetcher(argument)
            return await self._service.draft_from_parsed(parsed)

        await self._post_new_draft(fetch_and_draft)

    async def on_thread_message(
        self, *, channel: str | None, thread_ts: str | None, text: str, from_bot: bool
    ) -> None:
        if from_bot or channel != self._channel or thread_ts is None or not text.strip():
            return
        ref = f"{channel}:{thread_ts}"
        view = await self._service.find_by_draft_ref(ref)
        if view is None:
            return  # a reply in some other thread, not a draft
        message = text.strip()
        if is_email(message):
            await self._update_from(
                thread_ts, lambda: self._service.set_recipient(view.application_id, message)
            )
        else:
            await self._update_from(
                thread_ts, lambda: self._service.revise(view.application_id, message)
            )

    async def _post_new_draft(self, factory: Callable[[], Awaitable[DraftView]]) -> None:
        try:
            view = await factory()
        except (ApplicationStateError, LlmSchemaError) as err:
            await self._client.post_text(f"⚠️ {err}")
            return
        except Exception as err:
            logger.exception("drafting failed")
            await self._client.post_text(f"⚠️ Unerwarteter Fehler: {err}")
            return
        posted = await self._client.post_blocks(
            format_draft_blocks(view), draft_fallback_text(view)
        )
        if posted is None:
            await self._client.post_text("⚠️ Entwurf konnte nicht gepostet werden.")
            return
        await self._service.record_draft_ref(view.application_id, posted.ref)

    async def _update_from(
        self, message_ts: str, action: Callable[[], Awaitable[DraftView]]
    ) -> None:
        try:
            view = await action()
        except (ApplicationStateError, LlmSchemaError, EmailSendError) as err:
            await self._client.post_text(f"⚠️ {err}", thread_ts=message_ts)
            return
        except Exception as err:
            logger.exception("draft update failed")
            await self._client.post_text(f"⚠️ Unerwarteter Fehler: {err}", thread_ts=message_ts)
            return
        await self._client.update_blocks(
            self._channel, message_ts, format_draft_blocks(view), draft_fallback_text(view)
        )

    async def _send_application(self, application_id: int, message_ts: str) -> None:
        try:
            view = await self._service.send(application_id)
        except (ApplicationStateError, EmailSendError) as err:
            await self._client.post_text(f"⚠️ {err}", thread_ts=message_ts)
            return
        except Exception as err:
            logger.exception("sending application %d failed", application_id)
            await self._client.post_text(f"⚠️ Unerwarteter Fehler: {err}", thread_ts=message_ts)
            return
        await self._client.update_blocks(
            self._channel, message_ts, format_draft_blocks(view), draft_fallback_text(view)
        )
        await self._client.post_text(
            f"✅ Bewerbung verschickt an *{view.recipient}*\n"
            f"💬 LinkedIn-Nachricht zum Kopieren:\n```{view.linkedin_message}```",
            thread_ts=message_ts,
        )


async def run_socket_mode(  # pragma: no cover - network boundary
    *, bot: SlackBot, app_token: str, web_client: object, stop: object
) -> None:
    """Connect Socket Mode and feed envelopes into ``bot.dispatch`` until ``stop``."""
    import asyncio

    from slack_sdk.socket_mode.aiohttp import SocketModeClient
    from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web.async_client import AsyncWebClient

    assert isinstance(web_client, AsyncWebClient)
    assert isinstance(stop, asyncio.Event)
    socket = SocketModeClient(app_token=app_token, web_client=web_client)

    async def _handle(client: AsyncBaseSocketModeClient, request: SocketModeRequest) -> None:
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        try:
            await bot.dispatch(request.type, request.payload)
        except Exception:
            logger.exception("failed to process slack envelope %s", request.type)

    socket.socket_mode_request_listeners.append(_handle)
    logger.info("slack bot started (socket mode)")
    await socket.connect()  # type: ignore[no-untyped-call]
    try:
        await stop.wait()
    finally:
        await socket.disconnect()  # type: ignore[no-untyped-call]
        logger.info("slack bot stopped")
