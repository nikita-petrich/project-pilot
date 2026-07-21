"""Lean Telegram Bot API client (sendMessage) over httpx."""

import html
import logging
from dataclasses import dataclass
from typing import Self

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MatchMessage:
    """Display fields for one matched listing in a Telegram notification."""

    title: str
    url: str
    score: int
    reasons: list[str]
    start: str | None
    location: str | None
    remote: str


def format_match(message: MatchMessage) -> str:
    """One match as a compact HTML block (title link, score, top reasons, meta)."""
    link = f'<a href="{html.escape(message.url, quote=True)}">{html.escape(message.title)}</a>'
    lines = [f"⭐ <b>{link}</b>", f"Score: {message.score}"]
    lines.extend(f"• {html.escape(reason)}" for reason in message.reasons[:3])
    meta: list[str] = []
    if message.start:
        meta.append(f"Start: {html.escape(message.start)}")
    if message.location:
        meta.append(f"Location: {html.escape(message.location)}")
    meta.append(f"Remote: {html.escape(message.remote)}")
    lines.append(" | ".join(meta))
    return "\n".join(lines)


def build_digest(messages: list[MatchMessage]) -> str:
    """Batch a run's matches into one HTML message (empty string if none)."""
    if not messages:
        return ""
    plural = "es" if len(messages) != 1 else ""
    header = f"🔔 <b>{len(messages)} new match{plural}</b>"
    blocks = [format_match(message) for message in messages]
    return header + "\n\n" + "\n\n".join(blocks)


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
