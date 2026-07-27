"""Slack bot: routes Block-Kit button actions, ``/apply``, and thread replies.

The routing is pure and unit-tested; the Socket Mode connection that feeds it is
wired in ``cli.py`` (network boundary). Only the configured channel is served, and
every state change is guarded in the service layer.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from project_pilot.application.documents import extract_document_text
from project_pilot.application.service import DraftView, is_email
from project_pilot.enrichment.schemas import ContactEnrichment
from project_pilot.errors import (
    ApplicationStateError,
    EmailSendError,
    EnrichmentError,
    LlmSchemaError,
)
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.notification.slack import (
    Block,
    PostedMessage,
    contact_fallback_text,
    draft_fallback_text,
    format_contact_blocks,
    format_draft_blocks,
    status_blocks,
)

logger = logging.getLogger(__name__)

USAGE = "Usage: `/apply <freelancermap link or project description>`"


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


class EnrichmentFlow(Protocol):
    """The contact-enrichment surface the bot drives (optional)."""

    async def enrich_listing(self, listing_id: int) -> ContactEnrichment: ...


type ListingFetcher = Callable[[str], Awaitable[ParsedListing]]
type FileReader = Callable[[str], Awaitable[bytes]]


# Slack auto-links addresses and URLs in message text: an e-mail becomes
# ``<mailto:a@b|a@b>`` and a link ``<https://x|label>``. Reduce those to their
# plain target so recipient detection and revision text see clean input.
_SLACK_LINK_RE = re.compile(r"<(?:mailto:)?([^|>]+)(?:\|[^>]*)?>")


def _unwrap_slack_links(text: str) -> str:
    return _SLACK_LINK_RE.sub(lambda match: match.group(1), text)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _download_url(file: dict[str, object]) -> str | None:
    """Slack's authenticated download link for an uploaded file."""
    return _text(file.get("url_private_download")) or _text(file.get("url_private"))


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
        file_reader: FileReader | None = None,
        enrichment: EnrichmentFlow | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._service = service
        self._fetcher = fetcher
        self._file_reader = file_reader
        self._enrichment = enrichment

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
        message = _mapping(payload.get("message"))
        message_ts = _text(message.get("ts"))
        thread_ts = _text(message.get("thread_ts"))
        actions = payload.get("actions")
        if channel is None or message_ts is None or not isinstance(actions, list):
            return
        for action in actions:
            action_map = _mapping(action)
            action_id = _text(action_map.get("action_id"))
            value = _text(action_map.get("value"))
            if action_id is not None:
                await self.on_block_action(action_id, value, channel, message_ts, thread_ts)

    async def _dispatch_event(self, payload: dict[str, object]) -> None:
        event = _mapping(payload.get("event"))
        if event.get("type") != "message":
            return
        from_bot = bool(event.get("bot_id"))
        files = event.get("files")
        if not from_bot and isinstance(files, list) and files:
            await self.on_file_share(
                channel=_text(event.get("channel")),
                files=[_mapping(item) for item in files],
            )
            return
        # A file-share message also carries a subtype; only text replies flow on.
        await self.on_thread_message(
            channel=_text(event.get("channel")),
            thread_ts=_text(event.get("thread_ts")),
            text=_text(event.get("text")) or "",
            from_bot=from_bot or event.get("subtype") is not None,
        )

    async def _dispatch_command(self, payload: dict[str, object]) -> None:
        if _text(payload.get("command")) != "/apply":
            return
        await self.on_slash_apply(
            channel_id=_text(payload.get("channel_id")),
            text=_text(payload.get("text")) or "",
        )

    async def on_block_action(
        self,
        action_id: str,
        value: str | None,
        channel: str,
        message_ts: str,
        thread_ts: str | None = None,
    ) -> None:
        if channel != self._channel or value is None or not value.isdigit():
            return
        target = int(value)
        root = thread_ts or message_ts  # the thread everything for this draft lives in
        if action_id == "apply":
            await self._post_new_draft(
                lambda: self._service.draft_for_listing(target),
                root,
                progress="⏳ Creating application draft …",
            )
        elif action_id == "send":
            await self._send_application(target, draft_ts=message_ts, thread_root=root)
        elif action_id == "cancel":
            await self._cancel_application(target, draft_ts=message_ts, thread_root=root)
        elif action_id == "enrich":
            await self._run_enrichment(target, thread_root=root)
        # open_mail / open_project / open_li_* / open_google are URL buttons handled by Slack.

    async def on_slash_apply(self, channel_id: str | None, text: str) -> None:
        argument = text.strip()
        if not argument:
            await self._client.post_text(USAGE)
            return
        factory = await self._resolve_apply(argument)
        if factory is None:
            return  # a hint was already posted
        parent = await self._client.post_text(f"📥 Application: {argument[:150]}")
        if parent is None:
            return
        await self._post_new_draft(factory, parent.ts, progress="⏳ Creating application draft …")

    async def _resolve_apply(self, argument: str) -> Callable[[], Awaitable[DraftView]] | None:
        if not argument.lower().startswith(("http://", "https://")):
            return lambda: self._service.draft_from_text(argument)
        listing_id = await self._service.find_listing_id_by_url(argument)
        if listing_id is not None:
            return lambda: self._service.draft_for_listing(listing_id)
        if "freelancermap." not in argument or self._fetcher is None:
            await self._client.post_text(
                "⚠️ I don't recognize this link. Use `/apply` with the project description as text."
            )
            return None
        fetcher = self._fetcher

        async def fetch_and_draft() -> DraftView:
            parsed = await fetcher(argument)
            return await self._service.draft_from_parsed(parsed)

        return fetch_and_draft

    async def on_file_share(self, *, channel: str | None, files: list[dict[str, object]]) -> None:
        """Draft from an uploaded file (PDF or text) exactly like ``/apply <text>``."""
        if channel != self._channel or self._file_reader is None:
            return
        picked = next(
            ((file, url) for file in files if (url := _download_url(file)) is not None), None
        )
        if picked is None:
            return
        file, url = picked
        name = _text(file.get("name")) or "upload"
        parent = await self._client.post_text(f"📥 Application from file: {name[:150]}")
        if parent is None:
            return
        reader = self._file_reader

        async def factory() -> DraftView:
            data = await reader(url)
            text = await asyncio.to_thread(extract_document_text, name, data)
            return await self._service.draft_from_text(text)

        await self._post_new_draft(
            factory, parent.ts, progress="⏳ Reading file and creating draft …"
        )

    async def on_thread_message(
        self, *, channel: str | None, thread_ts: str | None, text: str, from_bot: bool
    ) -> None:
        if from_bot or channel != self._channel or thread_ts is None or not text.strip():
            return
        view = await self._service.find_by_draft_ref(f"{channel}:{thread_ts}")
        if view is None:
            return  # a reply in some other thread, not a draft
        message = _unwrap_slack_links(text).strip()
        if is_email(message):
            await self._post_new_draft(
                lambda: self._service.set_recipient(view.application_id, message),
                thread_ts,
                progress="✅ Setting recipient …",
            )
        else:
            await self._post_new_draft(
                lambda: self._service.revise(view.application_id, message),
                thread_ts,
                progress="✏️ Revising the draft …",
            )

    async def _post_new_draft(
        self,
        factory: Callable[[], Awaitable[DraftView]],
        thread_ts: str,
        *,
        progress: str,
    ) -> None:
        # Post a progress placeholder immediately, then update it in place to the
        # finished draft — instant feedback without an extra lingering message.
        placeholder = await self._client.post_text(progress, thread_ts=thread_ts)
        try:
            view = await factory()
        except (ApplicationStateError, LlmSchemaError) as err:
            await self._replace(placeholder, thread_ts, f"⚠️ {err}")
            return
        except Exception as err:
            logger.exception("drafting failed")
            await self._replace(placeholder, thread_ts, f"⚠️ Unexpected error: {err}")
            return
        blocks = format_draft_blocks(view)
        fallback = draft_fallback_text(view)
        if placeholder is not None:
            await self._client.update_blocks(placeholder.channel, placeholder.ts, blocks, fallback)
        else:
            await self._client.post_blocks(blocks, fallback, thread_ts=thread_ts)
        # Routing key is the thread root, so any reply in this thread reaches the draft.
        await self._service.record_draft_ref(view.application_id, f"{self._channel}:{thread_ts}")

    async def _send_application(
        self, application_id: int, *, draft_ts: str, thread_root: str
    ) -> None:
        progress = await self._client.post_text("⏳ Sending e-mail …", thread_ts=thread_root)
        try:
            view = await self._service.send(application_id)
        except (ApplicationStateError, EmailSendError) as err:
            await self._replace(progress, thread_root, f"⚠️ {err}")
            return
        except Exception as err:
            logger.exception("sending application %d failed", application_id)
            await self._replace(progress, thread_root, f"⚠️ Unexpected error: {err}")
            return
        await self._client.update_blocks(
            self._channel, draft_ts, format_draft_blocks(view), draft_fallback_text(view)
        )
        await self._replace(
            progress,
            thread_root,
            f"✅ Application sent to *{view.recipient}*\n"
            f"💬 LinkedIn message to copy:\n```{view.linkedin_message}```",
        )

    async def _run_enrichment(self, listing_id: int, *, thread_root: str) -> None:
        if self._enrichment is None:
            await self._client.post_text(
                "🔎 Contact research is off. Set `ENRICHMENT_ENABLED=true` to enable it.",
                thread_ts=thread_root,
            )
            return
        progress = await self._client.post_text(
            "🔎 Searching the company website for contact data …", thread_ts=thread_root
        )
        try:
            enrichment = await self._enrichment.enrich_listing(listing_id)
        except EnrichmentError as err:
            await self._replace(progress, thread_root, f"⚠️ {err}")
            return
        except Exception as err:
            logger.exception("enrichment failed for listing %d", listing_id)
            await self._replace(progress, thread_root, f"⚠️ Unexpected error: {err}")
            return
        blocks = format_contact_blocks(enrichment)
        fallback = contact_fallback_text(enrichment)
        if progress is not None:
            await self._client.update_blocks(progress.channel, progress.ts, blocks, fallback)
        else:
            await self._client.post_blocks(blocks, fallback, thread_ts=thread_root)

    async def _cancel_application(
        self, application_id: int, *, draft_ts: str, thread_root: str
    ) -> None:
        try:
            view = await self._service.cancel(application_id)
        except ApplicationStateError as err:
            await self._client.post_text(f"⚠️ {err}", thread_ts=thread_root)
            return
        except Exception as err:
            logger.exception("cancelling application %d failed", application_id)
            await self._client.post_text(f"⚠️ Unexpected error: {err}", thread_ts=thread_root)
            return
        await self._client.update_blocks(
            self._channel, draft_ts, format_draft_blocks(view), draft_fallback_text(view)
        )
        await self._client.post_text("❌ Draft discarded.", thread_ts=thread_root)

    async def _replace(self, posted: PostedMessage | None, thread_ts: str, text: str) -> None:
        """Turn a progress placeholder into its final text, or post it fresh."""
        if posted is not None:
            await self._client.update_blocks(posted.channel, posted.ts, status_blocks(text), text)
        else:
            await self._client.post_text(text, thread_ts=thread_ts)


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
