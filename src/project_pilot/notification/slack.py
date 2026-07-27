"""Slack Block Kit message building and a thin async Web API client wrapper.

One message carries everything: a match posts its full listing plus an apply
button; a draft posts the complete e-mail (split across ``section`` blocks so it is
never truncated), the LinkedIn text, and Send/Discard/Open-in-mail buttons.

All bot chrome (labels, buttons, hints, status) is English; only the generated
application text follows the project's language.
"""

import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from project_pilot.application.schemas import LINKEDIN_LIMIT
from project_pilot.application.service import DraftView
from project_pilot.models import ApplicationStatus
from project_pilot.notification.messages import MatchMessage

logger = logging.getLogger(__name__)

# Slack caps a section's text at 3000 chars and a message at 50 blocks; stay under.
_SECTION_LIMIT = 2900
_HEADER_LIMIT = 150
# Match alerts show a compact description preview; the full text stays one click
# away behind the "Zum Projekt" link (Slack has no native collapse/expand).
_DESCRIPTION_PREVIEW = 700

type Block = dict[str, object]


def _esc(text: str) -> str:
    """Escape the three characters Slack mrkdwn reserves (``&`` first)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _header(text: str) -> Block:
    trimmed = text if len(text) <= _HEADER_LIMIT else text[: _HEADER_LIMIT - 1] + "…"
    return {"type": "header", "text": {"type": "plain_text", "text": trimmed, "emoji": True}}


def _section(mrkdwn: str) -> Block:
    return {"type": "section", "text": {"type": "mrkdwn", "text": mrkdwn}}


def _context(mrkdwn: str) -> Block:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": mrkdwn}]}


def _button(
    text: str, *, action_id: str, value: str | None = None, url: str | None = None
) -> Block:
    element: Block = {
        "type": "button",
        "text": {"type": "plain_text", "text": text, "emoji": True},
        "action_id": action_id,
    }
    if value is not None:
        element["value"] = value
    if url is not None:
        element["url"] = url
    return element


def _link(url: str, label: str) -> str:
    return f"<{url}|{_esc(label)}>"


def _split_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into ``<= limit`` chunks on line boundaries; nothing dropped."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def status_blocks(text: str) -> list[Block]:
    """A single mrkdwn section — used for progress/confirmation/error messages."""
    return [_section(text)]


def _code_sections(text: str, *, label: str) -> list[Block]:
    """Render ``text`` as one or more copyable code-block sections under ``label``.

    Long text is split across consecutive sections (Slack caps a section at 3000
    chars); the pieces render as one continuous block, with no part counter.
    """
    parts = _split_text(text.strip() or "(leer)", _SECTION_LIMIT - len(label) - 12)
    blocks: list[Block] = []
    for index, part in enumerate(parts):
        prefix = f"*{label}*\n" if index == 0 else ""
        blocks.append(_section(f"{prefix}```{_esc(part)}```"))
    return blocks


def _description_block(description: str) -> Block:
    """A compact, copyable description preview; the full text stays behind the link.

    Slack messages have no native collapse, so a long freelancermap description is
    trimmed on a word boundary and marked as shortened — the ``Zum Projekt`` link
    carries the complete text.
    """
    text = description.strip()
    if len(text) > _DESCRIPTION_PREVIEW:
        cut = text.rfind(" ", _DESCRIPTION_PREVIEW // 2, _DESCRIPTION_PREVIEW)
        text = text[: cut if cut != -1 else _DESCRIPTION_PREVIEW].rstrip() + " …"
        suffix = "\n_Shortened — full description via 🔗 View project._"
    else:
        suffix = ""
    return _section(f"*📄 Description*\n```{_esc(text)}```{suffix}")


def _labeled(label: str, value: str | None) -> str | None:
    return f"*{label}:* {_esc(value)}" if value else None


def _labeled_list(label: str, values: list[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return f"*{label}:* {_esc(', '.join(picked))}" if picked else None


def format_match_blocks(message: MatchMessage, *, listing_id: int) -> list[Block]:
    """Build the Block Kit message for one matched listing (with an apply button)."""
    blocks: list[Block] = [_header(f"🎯 {message.title} · {message.score}/100")]

    who = None
    if message.is_endcustomer is not None:
        who = "Direct client" if message.is_endcustomer else "Agency"
    facts = [
        _labeled("🏢 Company", message.company),
        _labeled("👤 Contact", message.contact_name),
        _labeled("🤝 Client type", who),
        _labeled("📍 Location", message.location),
        _labeled("🏠 Remote", message.remote_label),
        _labeled("💼 Contract", message.contract_type),
        _labeled("📊 Workload", message.workload_label),
        _labeled("⏳ Duration", message.duration_label),
        _labeled("📅 Start", message.start),
        _labeled("🕒 Posted", message.posted_ago),
        _labeled("✍️ Apply by", message.expires_label),
        _labeled("🏭 Industry", message.industry or "unknown"),  # always shown
        _labeled("🗣 Language", message.language),
        _labeled_list("🛠 Skills", message.skills, limit=12),
    ]
    facts_text = "\n".join(fact for fact in facts if fact)
    if facts_text:
        blocks.append(_section(facts_text))

    verdict = [
        _labeled_list("✅ Fits", message.reasons, limit=3),
        _labeled_list("🎯 Your skills", message.matching_skills, limit=8),
        _labeled_list("⚠️ Gaps", message.missing_requirements, limit=4),
        _labeled_list("🚩 Risks", message.risk_flags, limit=3),
    ]
    verdict_text = "\n".join(item for item in verdict if item)
    if verdict_text:
        blocks.append(_section(verdict_text))

    if message.description:
        blocks.append(_description_block(message.description))

    actions: list[Block] = [
        _button("📝 Apply", action_id="apply", value=str(listing_id)),
        _button("📊 Add to Notion", action_id="notion_lead", value=str(listing_id)),
    ]
    if message.url:
        actions.append(_button("🔗 View project", action_id="open_project", url=message.url))
    blocks.append({"type": "actions", "elements": actions})
    return blocks


def format_draft_blocks(view: DraftView) -> list[Block]:
    """Build the Block Kit draft: full e-mail, LinkedIn, and the review buttons."""
    header_line = f"📨 Application draft: {view.title}"
    blocks: list[Block] = [_header(header_line)]

    meta = [_link(view.url, "View project")] if view.url else []
    meta.append(f"*To:* {_esc(view.recipient) if view.recipient else '❓ unknown'}")
    meta.append(f"*Subject:* `{_esc(view.subject)}`")
    blocks.append(_section("\n".join(meta)))

    blocks.extend(_code_sections(view.body, label="📄 E-mail (copy)"))
    blocks.extend(
        _code_sections(
            view.linkedin_message,
            label=f"💬 LinkedIn ({len(view.linkedin_message)}/{LINKEDIN_LIMIT})",
        )
    )

    actions: list[Block] = []
    if view.status in (ApplicationStatus.READY, ApplicationStatus.AWAITING_EMAIL):
        if view.recipient:
            actions.append(_button("📤 Send", action_id="send", value=str(view.application_id)))
        # "Open in mail client" is always offered — a missing recipient is simply
        # left blank in the mailto and filled in the mail client.
        mailto = f"mailto:{view.recipient or ''}?subject={quote(view.subject)}"
        actions.append(_button("📧 Open in mail client", action_id="open_mail", url=mailto))
        actions.append(_button("❌ Discard", action_id="cancel", value=str(view.application_id)))
    if view.status in (
        ApplicationStatus.READY,
        ApplicationStatus.AWAITING_EMAIL,
        ApplicationStatus.SENT,
    ):
        # Stays available after sending: a sent application files as "Contacted".
        actions.append(
            _button("📊 Add to Notion", action_id="notion_lead_app", value=str(view.application_id))
        )
    if actions:
        blocks.append({"type": "actions", "elements": actions})

    hints: list[str] = []
    if view.status is ApplicationStatus.AWAITING_EMAIL:
        hints.append("❗ No recipient detected — reply in the thread with the e-mail address.")
    if view.revision_count:
        hints.append(f"🔁 Revision #{view.revision_count}")
    hints.append(
        "✏️ Reply in the thread: free text = revise the draft · a lone e-mail "
        "address = set the recipient (then 📤 Send appears). Hover a code block to copy."
    )
    blocks.append(_context(" · ".join(hints)))
    return blocks


def match_fallback_text(message: MatchMessage) -> str:
    return f"🎯 New match: {message.title} ({message.score}/100)"


def draft_fallback_text(view: DraftView) -> str:
    return f"📨 Application draft: {view.title}"


@dataclass(frozen=True, slots=True)
class PostedMessage:
    """Where a posted Slack message lives (its channel and thread timestamp)."""

    channel: str
    ts: str

    @property
    def ref(self) -> str:
        """Stable ``channel:ts`` reference stored for thread-reply routing."""
        return f"{self.channel}:{self.ts}"


class SlackResponse(Protocol):
    def get(self, key: str, default: object = None, /) -> object: ...


class SlackWebClient(Protocol):
    """The subset of ``slack_sdk`` ``AsyncWebClient`` used here (fakeable in tests)."""

    async def chat_postMessage(  # noqa: N802 - mirrors slack_sdk's method name
        self,
        *,
        channel: str,
        text: str,
        blocks: list[Block] | None = None,
        thread_ts: str | None = None,
        unfurl_links: bool = True,
        unfurl_media: bool = True,
    ) -> SlackResponse: ...

    async def chat_update(
        self,
        *,
        channel: str,
        ts: str,
        text: str,
        blocks: list[Block] | None = None,
    ) -> SlackResponse: ...


class SlackClient:
    """Posts and updates Slack messages; errors are logged and surface as ``None``."""

    def __init__(self, *, channel: str, web_client: SlackWebClient) -> None:
        self._channel = channel
        self._web = web_client

    async def post_blocks(
        self, blocks: list[Block], text: str, *, thread_ts: str | None = None
    ) -> PostedMessage | None:
        return await self._post(text=text, blocks=blocks, thread_ts=thread_ts)

    async def post_text(self, text: str, *, thread_ts: str | None = None) -> PostedMessage | None:
        return await self._post(text=text, blocks=None, thread_ts=thread_ts)

    async def update_blocks(self, channel: str, ts: str, blocks: list[Block], text: str) -> bool:
        """Replace an existing message's blocks in place (draft revisions/send/cancel)."""
        try:
            response = await self._web.chat_update(channel=channel, ts=ts, text=text, blocks=blocks)
        except Exception as err:
            logger.warning("slack update failed: %s", err)
            return False
        if not response.get("ok"):
            logger.warning("slack update rejected: %s", response.get("error"))
            return False
        return True

    async def _post(
        self, *, text: str, blocks: list[Block] | None, thread_ts: str | None
    ) -> PostedMessage | None:
        try:
            response = await self._web.chat_postMessage(
                channel=self._channel,
                text=text,
                blocks=blocks,
                thread_ts=thread_ts,
                unfurl_links=False,  # no giant link previews cluttering the message
                unfurl_media=False,
            )
        except Exception as err:  # slack_sdk raises SlackApiError / transport errors
            logger.warning("slack post failed: %s", err)
            return None
        if not response.get("ok"):
            logger.warning("slack post rejected: %s", response.get("error"))
            return None
        channel = response.get("channel")
        ts = response.get("ts")
        if isinstance(channel, str) and isinstance(ts, str):
            return PostedMessage(channel=channel, ts=ts)
        return None


class SlackNotifier:
    """Pipeline notifier: posts match messages (with apply button) and warnings to Slack."""

    def __init__(self, client: SlackClient) -> None:
        self._client = client

    async def send_match(self, message: MatchMessage, *, listing_id: int) -> bool:
        posted = await self._client.post_blocks(
            format_match_blocks(message, listing_id=listing_id), match_fallback_text(message)
        )
        return posted is not None

    async def send_warning(self, text: str) -> bool:
        return await self._client.post_text(text) is not None
