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


def _esc(text: str) -> str:
    return html.escape(text)


def _link(url: str, label: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{_esc(label)}</a>'


def _search_link(url_prefix: str, query: str, label: str) -> str:
    return _link(f"{url_prefix}{quote(query)}", label)


def _join(prefix: str, values: list[str], *, limit: int) -> str | None:
    picked = [value for value in values if value][:limit]
    return f"{prefix}{_esc(', '.join(picked))}" if picked else None


def format_match(message: MatchMessage) -> str:
    """Render one match as a rich single Telegram HTML message."""
    header = f"🎯 <b>{_esc(message.title)}</b> · {message.score}/100"

    facts: list[str] = []
    if message.company:
        client = " · 🤝 Endkunde" if message.is_endcustomer else " · 🕵 Vermittler"
        facts.append(
            f"🏢 {_esc(message.company)}{client if message.is_endcustomer is not None else ''}"
        )
    place = [f"📍 {_esc(message.location)}"] if message.location else []
    if message.remote_label:
        place.append(f"🏠 {_esc(message.remote_label)}")
    if place:
        facts.append(" · ".join(place))
    contract = [f"💼 {_esc(message.contract_type)}"] if message.contract_type else []
    if message.workload_label:
        contract.append(f"📊 {_esc(message.workload_label)}")
    if message.duration_label:
        contract.append(f"⏳ {_esc(message.duration_label)}")
    if contract:
        facts.append(" · ".join(contract))
    timing = [f"📅 {_esc(message.start)}"] if message.start else []
    if message.posted_ago:
        timing.append(f"🕒 {_esc(message.posted_ago)}")
    if message.expires_label:
        timing.append(f"✍️ Bewerbung {_esc(message.expires_label)}")
    if timing:
        facts.append(" · ".join(timing))
    tail = [f"🏭 {_esc(message.industry)}"] if message.industry else []
    if message.language:
        tail.append(f"🗣 {_esc(message.language)}")
    if tail:
        facts.append(" · ".join(tail))
    if skills := _join("🛠 ", message.skills, limit=12):
        facts.append(skills)

    insight: list[str] = []
    if reasons := _join("✅ Passt: ", message.reasons, limit=3):
        insight.append(reasons)
    if match_skills := _join("🎯 Deine Skills: ", message.matching_skills, limit=8):
        insight.append(match_skills)
    gaps = _join("⚠️ Lücken: ", message.missing_requirements, limit=4)
    risks = _join("🚩 ", message.risk_flags, limit=3)
    if gaps and risks:
        insight.append(f"{gaps} · {risks}")
    elif gaps or risks:
        insight.append(gaps or risks or "")

    body: list[str] = []
    if message.description:
        text = message.description
        if len(text) > _DESCRIPTION_LIMIT:
            text = text[:_DESCRIPTION_LIMIT].rstrip() + " …"
        body.append(f"📄 <b>Beschreibung:</b>\n{_esc(text)}")

    links = [f"🔗 {_link(message.url, 'Zum Projekt')}"]
    if message.contact_name:
        links.append(
            "👤 "
            + _search_link(
                "https://www.linkedin.com/search/results/people/?keywords=",
                message.contact_name,
                f"Ansprechperson auf LinkedIn: {message.contact_name}",
            )
        )
    if message.company:
        links.append(
            "🔎 "
            + _search_link(
                "https://www.google.com/search?q=",
                message.company,
                f"Firma recherchieren: {message.company}",
            )
        )

    sections = [header, "\n".join(facts), "\n".join(insight), *body, "\n".join(links)]
    return "\n\n".join(section for section in sections if section)


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
