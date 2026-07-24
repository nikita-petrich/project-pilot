"""Slack Block Kit message building and a thin async Web API client wrapper.

One message carries everything: a match posts its full listing plus an apply
button; a draft posts the complete e-mail (split across ``section`` blocks so it is
never truncated), the LinkedIn text, and Senden/Verwerfen/Mail-öffnen buttons.
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


def _code_sections(text: str, *, label: str) -> list[Block]:
    """Render ``text`` as one or more copyable code-block sections under ``label``."""
    parts = _split_text(text.strip() or "(leer)", _SECTION_LIMIT - len(label) - 12)
    blocks: list[Block] = []
    for index, part in enumerate(parts):
        prefix = f"*{label}*\n" if index == 0 else ""
        suffix = f"\n_Teil {index + 1}/{len(parts)}_" if len(parts) > 1 else ""
        blocks.append(_section(f"{prefix}```{_esc(part)}```{suffix}"))
    return blocks


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
        who = "Endkunde" if message.is_endcustomer else "Vermittler"
    facts = [
        _labeled("🏢 Firma", message.company),
        _labeled("👤 Ansprechpartner", message.contact_name),
        _labeled("🤝 Auftraggeber", who),
        _labeled("📍 Einsatzort", message.location),
        _labeled("🏠 Remote", message.remote_label),
        _labeled("💼 Beschäftigungsart", message.contract_type),
        _labeled("📊 Auslastung", message.workload_label),
        _labeled("⏳ Dauer", message.duration_label),
        _labeled("📅 Start", message.start),
        _labeled("🕒 Eingestellt", message.posted_ago),
        _labeled("✍️ Bewerbung bis", message.expires_label),
        _labeled("🏭 Branche", message.industry),
        _labeled("🗣 Sprache", message.language),
        _labeled_list("🛠 Skills", message.skills, limit=12),
    ]
    facts_text = "\n".join(fact for fact in facts if fact)
    if facts_text:
        blocks.append(_section(facts_text))

    verdict = [
        _labeled_list("✅ Passt", message.reasons, limit=3),
        _labeled_list("🎯 Deine Skills", message.matching_skills, limit=8),
        _labeled_list("⚠️ Lücken", message.missing_requirements, limit=4),
        _labeled_list("🚩 Risiken", message.risk_flags, limit=3),
    ]
    verdict_text = "\n".join(item for item in verdict if item)
    if verdict_text:
        blocks.append(_section(verdict_text))

    if message.description:
        blocks.extend(_code_sections(message.description, label="📄 Beschreibung"))

    actions: list[Block] = [_button("📝 Bewerben", action_id="apply", value=str(listing_id))]
    if message.url:
        actions.append(_button("🔗 Zum Projekt", action_id="open_project", url=message.url))
    blocks.append({"type": "actions", "elements": actions})
    return blocks


def format_draft_blocks(view: DraftView) -> list[Block]:
    """Build the Block Kit draft: full e-mail, LinkedIn, and the review buttons."""
    header_line = f"📨 Bewerbungsentwurf: {view.title}"
    blocks: list[Block] = [_header(header_line)]

    meta = [_link(view.url, "Zum Projekt")] if view.url else []
    meta.append(f"*An:* {_esc(view.recipient) if view.recipient else '❓ unbekannt'}")
    meta.append(f"*Betreff:* `{_esc(view.subject)}`")
    blocks.append(_section("\n".join(meta)))

    blocks.extend(_code_sections(view.body, label="📄 E-Mail (zum Kopieren)"))
    blocks.extend(
        _code_sections(
            view.linkedin_message,
            label=f"💬 LinkedIn ({len(view.linkedin_message)}/{LINKEDIN_LIMIT})",
        )
    )

    if view.status in (ApplicationStatus.READY, ApplicationStatus.AWAITING_EMAIL):
        actions: list[Block] = []
        if view.recipient:
            actions.append(_button("📤 Senden", action_id="send", value=str(view.application_id)))
            mailto = f"mailto:{view.recipient}?subject={quote(view.subject)}"
            actions.append(_button("📧 Im Mail-Client öffnen", action_id="open_mail", url=mailto))
        actions.append(_button("❌ Verwerfen", action_id="cancel", value=str(view.application_id)))
        blocks.append({"type": "actions", "elements": actions})

    hints: list[str] = []
    if view.status is ApplicationStatus.AWAITING_EMAIL:
        hints.append("❗ Kein Empfänger gefunden - antworte im Thread mit der E-Mail-Adresse.")
    if view.revision_count:
        hints.append(f"🔁 Überarbeitung #{view.revision_count}")
    hints.append("✏️ Antworte im Thread, um Änderungen zu beschreiben.")
    blocks.append(_context(" · ".join(hints)))
    return blocks


def match_fallback_text(message: MatchMessage) -> str:
    return f"🎯 Neuer Match: {message.title} ({message.score}/100)"


def draft_fallback_text(view: DraftView) -> str:
    return f"📨 Bewerbungsentwurf: {view.title}"


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
                channel=self._channel, text=text, blocks=blocks, thread_ts=thread_ts
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
