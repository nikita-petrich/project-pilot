"""Lean Telegram Bot API client (sendMessage) over httpx."""

import html
import logging
from dataclasses import dataclass, field
from typing import Self
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

_DESCRIPTION_LIMIT = 2500


@dataclass(frozen=True, slots=True)
class MatchMessage:
    """Display-ready fields for one matched listing (all values pre-formatted)."""

    title: str
    url: str
    score: int
    company: str | None = None
    contact_name: str | None = None
    is_endcustomer: bool | None = None
    location: str | None = None
    remote_label: str | None = None
    contract_type: str | None = None
    workload_label: str | None = None
    duration_label: str | None = None
    start: str | None = None
    posted_ago: str | None = None
    expires_label: str | None = None
    industry: str | None = None
    language: str | None = None
    skills: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    matching_skills: list[str] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    description: str = ""
    onsite_only: bool = False


def _esc(text: str) -> str:
    return html.escape(text)


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{_esc(label)}</a>'


def _search_link(url_prefix: str, query: str, label: str) -> str:
    return _link(f"{url_prefix}{quote(query)}", label)


def _labeled(emoji: str, label: str, value: str | None) -> str | None:
    return f"{emoji} <b>{label}:</b> {_esc(value)}" if value else None


def _labeled_list(emoji: str, label: str, values: list[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return _labeled(emoji, label, ", ".join(picked)) if picked else None


_LINKEDIN_PEOPLE = "https://www.linkedin.com/search/results/people/?keywords="
_GOOGLE = "https://www.google.com/search?q="


def format_match(message: MatchMessage) -> str:
    """Render one match as a rich, fully labeled single Telegram HTML message."""
    lines: list[str] = [f"🎯 <b>{_esc(message.title)}</b> · {message.score}/100", ""]

    def add(line: str | None) -> None:
        if line:
            lines.append(line)

    add(_labeled("🏢", "Firma", message.company))
    if message.contact_name:
        link = _search_link(_LINKEDIN_PEOPLE, message.contact_name, message.contact_name)
        lines.append(f"👤 <b>Ansprechpartner:</b> {link}")
    if message.is_endcustomer is not None:
        who = "Endkunde" if message.is_endcustomer else "Vermittler"
        add(_labeled("🤝", "Auftraggeber", who))
    add(_labeled("📍", "Einsatzort", message.location))
    add(_labeled("🏠", "Remote", message.remote_label))
    add(_labeled("💼", "Beschäftigungsart", message.contract_type))
    add(_labeled("📊", "Auslastung", message.workload_label))
    add(_labeled("⏳", "Dauer", message.duration_label))
    add(_labeled("📅", "Start", message.start))
    add(_labeled("🕒", "Eingestellt", message.posted_ago))
    add(_labeled("✍️", "Bewerbung bis", message.expires_label))
    add(_labeled("🏭", "Branche", message.industry))
    add(_labeled("🗣", "Sprache", message.language))
    add(_labeled_list("🛠", "Skills", message.skills, limit=12))
    add(_labeled_list("✅", "Passt", message.reasons, limit=3))
    add(_labeled_list("🎯", "Deine Skills", message.matching_skills, limit=8))
    add(_labeled_list("⚠️", "Lücken", message.missing_requirements, limit=4))
    add(_labeled_list("🚩", "Risiken", message.risk_flags, limit=3))

    if message.description:
        text = message.description
        if len(text) > _DESCRIPTION_LIMIT:
            text = text[:_DESCRIPTION_LIMIT].rstrip() + " …"
        lines.append(f"\n📄 <b>Beschreibung:</b>\n{_esc(text)}")

    lines.append("")
    lines.append(f"🔗 {_link(message.url, 'Zum Projekt')}")
    if message.company:
        lines.append(f"🔎 {_search_link(_GOOGLE, message.company, 'Firma googeln')}")

    return "\n".join(lines)


class TelegramClient:
    """Minimal Bot API client: sendMessage with HTML, returning success as a bool."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._chat_id = chat_id
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_message(self, text: str, *, disable_preview: bool = True) -> bool:
        """Send an HTML message; return True only on a Bot API ``ok`` response."""
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        try:
            response = await self._client.post(f"{self._base_url}/sendMessage", json=payload)
        except httpx.HTTPError as err:
            logger.warning("telegram send failed (transport): %s", err)
            return False
        if response.status_code != 200:
            logger.warning("telegram send failed (status %s)", response.status_code)
            return False
        body = response.json()
        ok = bool(body.get("ok"))
        if not ok:
            logger.warning("telegram send rejected: %s", body.get("description"))
        return ok
