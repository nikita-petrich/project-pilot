"""Slack bot: routes Block-Kit button actions, ``/apply``, and thread replies.

Uploads work everywhere text does: a PDF/text/image dropped in the channel starts
a draft (the image path of ``/apply``, since slash commands cannot carry files),
and an image posted in a draft's thread feeds the next revision as vision input.

Every request costs the channel exactly one line: a slash command posts an anchor
and answers in its thread, an upload is answered in its own thread.

The routing is pure and unit-tested; the Socket Mode connection that feeds it is
wired in ``cli.py`` (network boundary). Only the configured channel is served, and
every state change is guarded in the service layer.
"""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from project_pilot.application.documents import (
    ImageAttachment,
    extract_document_text,
    is_image_mime_type,
)
from project_pilot.application.service import DraftView, is_email
from project_pilot.enrichment.schemas import ContactEnrichment
from project_pilot.errors import (
    ApplicationStateError,
    EmailSendError,
    EnrichmentError,
    LlmSchemaError,
    SelectorMismatchError,
    assert_defined,
)
from project_pilot.evaluation.check import CheckResult
from project_pilot.ingestion.normalize import extract_listing_title
from project_pilot.ingestion.parser import ParsedListing
from project_pilot.notification.slack import (
    Block,
    PostedMessage,
    check_fallback_text,
    contact_fallback_text,
    draft_fallback_text,
    format_check_blocks,
    format_contact_blocks,
    format_draft_blocks,
    format_upload_prompt_blocks,
    sent_confirmation_blocks,
    sent_fallback_text,
    status_blocks,
    upload_prompt_fallback_text,
)

logger = logging.getLogger(__name__)

USAGE = (
    "Usage: `/apply <freelancermap link or project description>` — or upload a "
    "screenshot/PDF and press 📝 Apply."
)
CHECK_USAGE = (
    "Usage: `/check <freelancermap link or project description>` — or upload a "
    "screenshot/PDF and press 🔍 Check."
)
# Both buttons are gone once used (the message is rewritten), so this only shows
# after a restart dropped the in-memory state.
UPLOAD_EXPIRED = "⚠️ I no longer have that upload (bot restart) — please upload the file again."

# Bounded per-message state: pending uploads awaiting their button, and checked
# inputs kept for a passing check's apply button. Losing either on restart only
# costs a button; `/apply` and `/check` always work.
_PENDING_CHECK_LIMIT = 50
_PENDING_UPLOAD_LIMIT = 50

# What an image-only thread reply means: fold the screenshot into the draft.
DEFAULT_IMAGE_INSTRUCTION = "Revise the draft taking the attached image(s) into account."

# More screenshots than this per message is almost certainly a mistake; cap the
# vision payload instead of uploading a whole gallery to the LLM.
_MAX_IMAGES = 5

# How long a channel-anchor label may get before it is cut.
_LABEL_LIMIT = 150


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


class CheckFlow(Protocol):
    """The check-service surface the bot drives (``CheckService`` satisfies it)."""

    async def check_stored(self, listing_id: int) -> CheckResult: ...
    async def check_parsed(self, parsed: ParsedListing) -> CheckResult: ...
    async def check_text(
        self, text: str, *, images: Sequence[ImageAttachment] = ()
    ) -> CheckResult: ...


class ApplicationFlow(Protocol):
    """The application-service surface the bot drives."""

    async def draft_for_listing(self, listing_id: int) -> DraftView: ...
    async def draft_from_parsed(self, parsed: ParsedListing) -> DraftView: ...
    async def draft_from_text(
        self, text: str, *, images: Sequence[ImageAttachment] = ()
    ) -> DraftView: ...
    async def revise(
        self, application_id: int, instruction: str, *, images: Sequence[ImageAttachment] = ()
    ) -> DraftView: ...
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
# A check factory yields the result plus the checked input (when there is one)
# so a passing text/file/image check can remember it for its apply button.
type CheckFactory = Callable[[], Awaitable[tuple[CheckResult, "_PendingCheck | None"]]]


# Slack auto-links addresses and URLs in message text: an e-mail becomes
# ``<mailto:a@b|a@b>`` and a link ``<https://x|label>``. Reduce those to their
# plain target so recipient detection and revision text see clean input.
_SLACK_LINK_RE = re.compile(r"<(?:mailto:)?([^|>]+)(?:\|[^>]*)?>")


def _unwrap_slack_links(text: str) -> str:
    return _SLACK_LINK_RE.sub(lambda match: match.group(1), text)


def _input_label(argument: str) -> str:
    """A readable channel-anchor label for a slash-command argument.

    A pasted description would be cut mid-sentence, so the anchor names the listing
    instead: its own headline when it has one, otherwise its size — the resolved
    title replaces this label as soon as the check or draft is done.
    """
    if argument.lower().startswith(("http://", "https://")):
        return argument[:_LABEL_LIMIT]
    heading = extract_listing_title(argument)
    if heading:
        return heading[:_LABEL_LIMIT]
    return f"pasted description ({len(argument)} characters)"


def _remember[T](store: dict[str, T], key: str, value: T, limit: int) -> None:
    """Keep bounded per-message state, evicting the oldest entry first."""
    store[key] = value
    while len(store) > limit:
        store.pop(next(iter(store)))


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _download_url(file: dict[str, object]) -> str | None:
    """Slack's authenticated download link for an uploaded file."""
    return _text(file.get("url_private_download")) or _text(file.get("url_private"))


@dataclass(frozen=True, slots=True)
class _ImageFile:
    """A supported image upload, not yet downloaded (name, type, and where from)."""

    name: str
    mime_type: str
    url: str


@dataclass(frozen=True, slots=True)
class _DocumentFile:
    """A non-image upload (PDF or text) whose text is extracted on use."""

    name: str
    url: str


@dataclass(frozen=True, slots=True)
class _PendingCheck:
    """What a passing check's apply button should draft from later.

    Images are kept as Slack file references, not bytes — they are re-downloaded
    when the button is tapped, so 50 remembered checks stay cheap.
    """

    text: str
    images: tuple[_ImageFile, ...] = ()


@dataclass(frozen=True, slots=True)
class _PendingUpload:
    """An upload waiting for its Apply/Check button; images or one document."""

    label: str
    caption: str
    images: tuple[_ImageFile, ...] = ()
    document: _DocumentFile | None = None


def _document_file(files: list[dict[str, object]]) -> _DocumentFile | None:
    """The first downloadable non-image upload, if any."""
    for file in files:
        url = _download_url(file)
        if url is not None and not is_image_mime_type(_text(file.get("mimetype"))):
            return _DocumentFile(name=_text(file.get("name")) or "upload", url=url)
    return None


def _image_files(files: list[dict[str, object]]) -> list[_ImageFile]:
    """The vision-capable image uploads among ``files`` (capped at ``_MAX_IMAGES``)."""
    picked: list[_ImageFile] = []
    for file in files:
        mime_type = _text(file.get("mimetype"))
        url = _download_url(file)
        if mime_type is None or not is_image_mime_type(mime_type) or url is None:
            continue
        picked.append(
            _ImageFile(name=_text(file.get("name")) or "screenshot", mime_type=mime_type, url=url)
        )
        if len(picked) == _MAX_IMAGES:
            break
    return picked


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
        checker: CheckFlow | None = None,
        enrichment: EnrichmentFlow | None = None,
    ) -> None:
        self._client = client
        self._channel = channel
        self._service = service
        self._fetcher = fetcher
        self._file_reader = file_reader
        self._checker = checker
        self._enrichment = enrichment
        self._pending_checks: dict[str, _PendingCheck] = {}
        self._pending_uploads: dict[str, _PendingUpload] = {}

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
                text=_text(event.get("text")) or "",
                thread_ts=_text(event.get("thread_ts")),
                message_ts=_text(event.get("ts")),
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
        command = _text(payload.get("command"))
        channel_id = _text(payload.get("channel_id"))
        text = _text(payload.get("text")) or ""
        if command == "/apply":
            await self.on_slash_apply(channel_id=channel_id, text=text)
        elif command == "/check":
            await self.on_slash_check(channel_id=channel_id, text=text)

    async def on_block_action(
        self,
        action_id: str,
        value: str | None,
        channel: str,
        message_ts: str,
        thread_ts: str | None = None,
    ) -> None:
        if channel != self._channel or value is None:
            return
        root = thread_ts or message_ts  # the thread everything for this draft lives in
        if action_id == "apply_url":  # a passing /check on a not-yet-stored listing URL
            factory = await self._resolve_apply(value, thread_ts=root)
            if factory is not None:
                await self._post_new_draft(
                    factory, root, progress="⏳ Creating application draft …"
                )
            return
        if action_id == "apply_check":  # a passing /check on pasted text or a file
            await self._apply_checked_text(value, root)
            return
        if action_id == "upload_apply":  # 📝 on an upload prompt
            await self._apply_upload(value, prompt_ts=message_ts, thread_root=root)
            return
        if action_id == "upload_check":  # 🔍 on an upload prompt
            await self._check_upload(value, prompt_ts=message_ts, thread_root=root)
            return
        if not value.isdigit():
            return
        target = int(value)
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
        # open_project / open_li_* / open_google are URL buttons handled by Slack itself.

    async def on_slash_apply(self, channel_id: str | None, text: str) -> None:
        argument = text.strip()
        if not argument:
            await self._client.post_text(USAGE)
            return
        # A slash command leaves no message in the channel: post one anchor for it and
        # keep every answer (hints, progress, draft) in that anchor's thread.
        parent = await self._client.post_text(f"📥 Application: {_input_label(argument)}")
        if parent is None:
            return
        factory = await self._resolve_apply(argument, thread_ts=parent.ts)
        if factory is None:
            return  # a hint was already posted in the thread
        await self._post_new_draft(
            factory,
            parent.ts,
            progress="⏳ Creating application draft …",
            anchor_ts=parent.ts,
        )

    async def _resolve_apply(
        self, argument: str, *, thread_ts: str | None
    ) -> Callable[[], Awaitable[DraftView]] | None:
        if not argument.lower().startswith(("http://", "https://")):
            return lambda: self._service.draft_from_text(argument)
        listing_id = await self._service.find_listing_id_by_url(argument)
        if listing_id is not None:
            return lambda: self._service.draft_for_listing(listing_id)
        if "freelancermap." not in argument or self._fetcher is None:
            await self._client.post_text(
                "⚠️ I don't recognize this link. Use `/apply` with the project description as text.",
                thread_ts=thread_ts,
            )
            return None
        fetcher = self._fetcher

        async def fetch_and_draft() -> DraftView:
            parsed = await fetcher(argument)
            return await self._service.draft_from_parsed(parsed)

        return fetch_and_draft

    async def on_slash_check(self, channel_id: str | None, text: str) -> None:
        if self._checker is None:
            return
        argument = text.strip()
        if not argument:
            await self._client.post_text(CHECK_USAGE)
            return
        # Same as ``/apply``: one anchor line in the channel, the verdict in its thread.
        label = _input_label(argument)
        parent = await self._client.post_text(f"🔍 Check: {label}")
        if parent is None:
            return
        resolved = await self._resolve_check(argument, thread_ts=parent.ts)
        if resolved is None:
            return  # a hint was already posted in the thread
        factory, apply_action, apply_value = resolved
        await self._run_check(
            factory,
            label=label,
            apply_action=apply_action,
            apply_value=apply_value,
            thread_ts=parent.ts,
            anchor_ts=parent.ts,
        )

    async def _resolve_check(
        self, argument: str, *, thread_ts: str | None
    ) -> tuple[CheckFactory, str | None, str | None] | None:
        """Turn the ``/check`` argument into a factory plus the apply-button routing."""
        checker = self._checker
        if checker is None:
            return None
        if not argument.lower().startswith(("http://", "https://")):

            async def from_text() -> tuple[CheckResult, _PendingCheck | None]:
                return await checker.check_text(argument), _PendingCheck(text=argument)

            return from_text, None, None
        listing_id = await self._service.find_listing_id_by_url(argument)
        if listing_id is not None:
            target = listing_id

            async def from_stored() -> tuple[CheckResult, _PendingCheck | None]:
                return await checker.check_stored(target), None

            return from_stored, "apply", str(target)
        if "freelancermap." not in argument or self._fetcher is None:
            await self._client.post_text(
                "⚠️ I don't recognize this link. Use `/check` with the project description as text.",
                thread_ts=thread_ts,
            )
            return None
        fetcher = self._fetcher

        async def fetch_and_check() -> tuple[CheckResult, _PendingCheck | None]:
            parsed = await fetcher(argument)
            return await checker.check_parsed(parsed), None

        return fetch_and_check, "apply_url", argument

    async def _run_check(
        self,
        factory: CheckFactory,
        *,
        label: str,
        apply_action: str | None,
        apply_value: str | None,
        target_ts: str | None = None,
        thread_ts: str | None = None,
        anchor_ts: str | None = None,
    ) -> None:
        """Run the check and render the verdict in place.

        ``target_ts`` reuses an existing message (an upload prompt) as the progress
        placeholder instead of posting a new one; ``thread_ts`` is the thread a fresh
        placeholder is posted into, so a verdict never lands loose in the channel.
        ``anchor_ts`` is the slash command's channel line, relabeled with the
        resolved project title once it is known.
        """
        progress = f"🔍 Checking against your profile: {label} …"
        if target_ts is None:
            placeholder = await self._client.post_text(progress, thread_ts=thread_ts)
        else:
            await self._client.update_blocks(
                self._channel, target_ts, status_blocks(progress), progress
            )
            placeholder = PostedMessage(channel=self._channel, ts=target_ts)
        try:
            result, pending = await factory()
        except (ApplicationStateError, SelectorMismatchError, LlmSchemaError) as err:
            await self._replace(placeholder, thread_ts, f"⚠️ {err}")
            return
        except Exception as err:
            logger.exception("check failed")
            await self._replace(placeholder, thread_ts, f"⚠️ Unexpected error: {err}")
            return
        if result.passed and pending is not None and placeholder is not None:
            self._remember_check(placeholder.ts, pending)
            apply_action, apply_value = "apply_check", placeholder.ts
        blocks = format_check_blocks(result, apply_action=apply_action, apply_value=apply_value)
        fallback = check_fallback_text(result)
        if placeholder is not None:
            await self._client.update_blocks(placeholder.channel, placeholder.ts, blocks, fallback)
        else:
            await self._client.post_blocks(blocks, fallback, thread_ts=thread_ts)
        await self._relabel_anchor(anchor_ts, "🔍 Check", result.title)

    def _remember_check(self, key: str, pending: _PendingCheck) -> None:
        """Keep a checked input so the result's apply button can draft from it later."""
        _remember(self._pending_checks, key, pending, _PENDING_CHECK_LIMIT)

    async def _apply_checked_text(self, key: str, thread_root: str) -> None:
        pending = self._pending_checks.get(key)
        if pending is None:
            await self._client.post_text(
                "⚠️ This check has expired (bot restart) — run `/apply` with the "
                "project text instead.",
                thread_ts=thread_root,
            )
            return

        async def factory() -> DraftView:
            return await self._service.draft_from_text(
                pending.text, images=await self._download_images(list(pending.images))
            )

        await self._post_new_draft(factory, thread_root, progress="⏳ Creating application draft …")

    async def on_file_share(
        self,
        *,
        channel: str | None,
        files: list[dict[str, object]],
        text: str = "",
        thread_ts: str | None = None,
        message_ts: str | None = None,
    ) -> None:
        """Route an upload (PDF, text, or image): revise a draft, or offer buttons.

        Inside a draft's thread the upload feeds that draft's revision — the thread
        already says which draft is meant. In the channel the intent is unknown, so
        the bot asks with 📝 Apply / 🔍 Check buttons; nothing runs (and no token is
        spent) until one is pressed. The prompt is posted as a reply to the upload, so
        the whole exchange stays in the upload's thread and the channel keeps one line.
        """
        if channel != self._channel or self._file_reader is None:
            return
        if thread_ts is not None:
            view = await self._service.find_by_draft_ref(f"{channel}:{thread_ts}")
            if view is not None:
                await self._revise_with_images(view, thread_ts=thread_ts, text=text, files=files)
                return
        if message_ts is None:
            return
        images = _image_files(files)
        document = _document_file(files) if not images else None
        if not images and document is None:
            return  # nothing downloadable — not an upload we can read
        if images:
            label = images[0].name if len(images) == 1 else f"{len(images)} images"
        else:
            label = assert_defined(document, "upload without image or document").name
        pending = _PendingUpload(
            label=label[:150],
            caption=_unwrap_slack_links(text).strip(),
            images=tuple(images),
            document=document,
        )
        # Keyed by the upload's own ts, which is known before the prompt is posted.
        _remember(self._pending_uploads, message_ts, pending, _PENDING_UPLOAD_LIMIT)
        await self._client.post_blocks(
            format_upload_prompt_blocks(
                pending.label, key=message_ts, can_check=self._checker is not None
            ),
            upload_prompt_fallback_text(pending.label),
            thread_ts=message_ts,
        )

    async def _apply_upload(self, key: str, *, prompt_ts: str, thread_root: str) -> None:
        """📝 Apply on an upload prompt: draft from the screenshot(s) or document.

        The draft hangs off ``thread_root`` (the upload itself), not off the prompt —
        the prompt is a reply, and the routing key must match the thread replies carry.
        """
        pending = self._pending_uploads.pop(key, None)
        if pending is None:
            await self._client.post_text(UPLOAD_EXPIRED, thread_ts=thread_root)
            return
        await self._consume_prompt(prompt_ts, f"📥 Application from {pending.label}")
        await self._post_new_draft(
            self._draft_factory(pending), thread_root, progress="⏳ Reading upload and drafting …"
        )

    async def _check_upload(self, key: str, *, prompt_ts: str, thread_root: str) -> None:
        """🔍 Check on an upload prompt: score it like the scanner would."""
        checker = self._checker
        if checker is None:
            return
        pending = self._pending_uploads.pop(key, None)
        if pending is None:
            await self._client.post_text(UPLOAD_EXPIRED, thread_ts=thread_root)
            return

        async def factory() -> tuple[CheckResult, _PendingCheck | None]:
            text, images = await self._upload_input(pending)
            result = await checker.check_text(text, images=images)
            return result, _PendingCheck(text=text, images=pending.images)

        await self._run_check(
            factory,
            label=pending.label,
            apply_action=None,
            apply_value=None,
            target_ts=prompt_ts,
            thread_ts=thread_root,
        )

    def _draft_factory(self, pending: _PendingUpload) -> Callable[[], Awaitable[DraftView]]:
        async def factory() -> DraftView:
            text, images = await self._upload_input(pending)
            return await self._service.draft_from_text(text, images=images)

        return factory

    async def _upload_input(self, pending: _PendingUpload) -> tuple[str, list[ImageAttachment]]:
        """Resolve a pending upload into LLM input: screenshots, or extracted text.

        Downloading is deferred to the button press, so an upload nobody acts on
        never costs a fetch.
        """
        if pending.images:
            return pending.caption, await self._download_images(list(pending.images))
        document = assert_defined(pending.document, "pending upload has no document")
        reader = assert_defined(self._file_reader, "no file reader configured")
        data = await reader(document.url)
        return await asyncio.to_thread(extract_document_text, document.name, data), []

    async def _consume_prompt(self, prompt_ts: str, text: str) -> None:
        """Rewrite the prompt message without its buttons so it cannot fire twice."""
        await self._client.update_blocks(self._channel, prompt_ts, status_blocks(text), text)

    async def _revise_with_images(
        self, view: DraftView, *, thread_ts: str, text: str, files: list[dict[str, object]]
    ) -> None:
        """An upload in a draft's thread: images become vision input for the revision."""
        message = _unwrap_slack_links(text).strip()
        if is_email(message):
            # The reply is really a recipient correction; the upload changes nothing.
            await self._post_new_draft(
                lambda: self._service.set_recipient(view.application_id, message),
                thread_ts,
                progress="✅ Setting recipient …",
            )
            return
        images = _image_files(files)
        if not images:
            await self._client.post_text(
                "⚠️ Only images (PNG/JPEG/WebP/GIF) can accompany a revision — "
                "please paste document content as text.",
                thread_ts=thread_ts,
            )
            return
        instruction = message or DEFAULT_IMAGE_INSTRUCTION

        async def factory() -> DraftView:
            return await self._service.revise(
                view.application_id, instruction, images=await self._download_images(images)
            )

        await self._post_new_draft(factory, thread_ts, progress="✏️ Revising the draft …")

    async def _download_images(self, images: list[_ImageFile]) -> list[ImageAttachment]:
        reader = self._file_reader
        if reader is None:  # guarded by every caller; keeps the types honest
            return []
        return [
            ImageAttachment(
                name=image.name, mime_type=image.mime_type, data=await reader(image.url)
            )
            for image in images
        ]

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

    async def _relabel_anchor(self, anchor_ts: str | None, prefix: str, title: str) -> None:
        """Rewrite a slash command's channel line with the resolved project title.

        The anchor is posted before the listing is understood, so it starts out with
        a heuristic label; this replaces it once the real title exists.
        """
        if anchor_ts is None or not title.strip():
            return
        text = f"{prefix}: {title.strip()[:_LABEL_LIMIT]}"
        await self._client.update_blocks(self._channel, anchor_ts, status_blocks(text), text)

    async def _post_new_draft(
        self,
        factory: Callable[[], Awaitable[DraftView]],
        thread_ts: str,
        *,
        progress: str,
        anchor_ts: str | None = None,
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
        await self._relabel_anchor(anchor_ts, "📥 Application", view.title)
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
        confirmation = sent_confirmation_blocks(view)
        fallback = sent_fallback_text(view)
        if progress is not None:
            await self._client.update_blocks(progress.channel, progress.ts, confirmation, fallback)
        else:
            await self._client.post_blocks(confirmation, fallback, thread_ts=thread_root)

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

    async def _replace(
        self, posted: PostedMessage | None, thread_ts: str | None, text: str
    ) -> None:
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
