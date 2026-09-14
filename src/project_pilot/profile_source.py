"""The public half of the profile, fetched from sequenz.io at runtime.

The website *is* the profile. Keeping a second copy in this repository meant two
versions of one truth, and the one nobody looks at is the one that goes stale —
so the profile is no longer maintained here. Two documents are read:

- ``/<locale>.md`` — the markdown twin of the profile page: positioning, key
  facts, skills, reference projects, testimonials. Generated on the site from the
  same content the page renders, so it cannot drift from what a human sees.
- ``/api/profile.json`` — the figures prose carries imprecisely: availability,
  capacity, the on-site ceiling, the rate, the booking links per language, the
  platform profiles. Those become the ``Availability & terms`` and
  ``Contact & Signature`` blocks below, rendered here rather than parsed out of
  the twin, because the application prompt reads them as exact ``Key: value``
  lines and a paragraph is the wrong place to look for a VAT id.

A failed fetch raises. It never falls back to an older copy: an evaluation
carries the hash of the profile it was judged against, and a silent fallback
would file a verdict under a profile that was not the one in force.
"""

import logging
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from project_pilot.errors import ProfileUnavailableError

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0
# Both documents are static files behind a CDN; three attempts covers a restart
# or a cold edge without turning a real outage into a long wait.
_ATTEMPTS = 3


class _Availability(BaseModel):
    model_config = ConfigDict(extra="ignore")

    from_: str = Field(alias="from")
    capacity_percent: int
    onsite_max_percent: int
    employee_leasing: bool = False


class _Rate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    currency: str = "EUR"
    basis: str = "from"
    hourly: int
    daily: int
    label: dict[str, str] = Field(default_factory=dict)


class _Contact(BaseModel):
    model_config = ConfigDict(extra="ignore")

    email: str
    phone: str
    web: str
    booking: dict[str, str] = Field(default_factory=dict)


class _Link(BaseModel):
    model_config = ConfigDict(extra="ignore")

    label: str
    href: str


class ProfileFeed(BaseModel):
    """``/api/profile.json`` — the machine-readable half, validated on arrival."""

    model_config = ConfigDict(extra="ignore")

    name: str
    role: str
    availability: _Availability
    rate: _Rate
    contact: _Contact
    location: dict[str, str] = Field(default_factory=dict)
    vat_id: str = ""
    profiles: list[_Link] = Field(default_factory=list)
    sentences: dict[str, dict[str, str]] = Field(default_factory=dict)

    def link(self, label: str) -> str | None:
        for entry in self.profiles:
            if entry.label.lower() == label.lower():
                return entry.href
        return None


@dataclass(frozen=True, slots=True)
class RemoteProfile:
    """What the website handed over: the twin's text, the feed, and where from."""

    markdown: str
    feed: ProfileFeed
    source_url: str


def _sentence(feed: ProfileFeed, kind: str, locale: str) -> str:
    return feed.sentences.get(kind, {}).get(locale, "")


def derived_sections(feed: ProfileFeed) -> str:
    """The two blocks the application prompt reads as exact ``Key: value`` lines.

    Rendered from the JSON rather than lifted out of the twin: the prompt asks for
    a VAT id, a booking link per language and a capacity percentage by name, and
    each of those has to be one unambiguous value, not a phrase inside a
    sentence.
    """
    onsite = feed.availability.onsite_max_percent
    leasing = "yes" if feed.availability.employee_leasing else "no"
    lines = [
        "## Availability & terms",
        "",
        f"- Available: {feed.availability.from_}",
        f"- Capacity: {feed.availability.capacity_percent}% (of which at most {onsite}% on-site)",
        f"- Employee leasing (Arbeitnehmerüberlassung): {leasing}",
        f"- Rate: {feed.rate.basis} {feed.rate.hourly} {feed.rate.currency}/h · "
        f"{feed.rate.basis} {feed.rate.daily} {feed.rate.currency}/day · project-dependent",
        f"- Availability sentence (German): {_sentence(feed, 'availability', 'de')}",
        f"- Availability sentence (English): {_sentence(feed, 'availability', 'en')}",
        f"- Rate sentence (German): {_sentence(feed, 'rate', 'de')}",
        f"- Rate sentence (English): {_sentence(feed, 'rate', 'en')}",
        "",
        "## Contact & Signature",
        "",
        feed.name,
        f"Title: {feed.role}",
        f"Phone: {feed.contact.phone}",
        f"Email: {feed.contact.email}",
        f"Web: {feed.contact.web}",
    ]
    for label in ("LinkedIn", "GitHub"):
        href = feed.link(label)
        if href:
            lines.append(f"{label}: {href}")
    lines += [
        f"CTA German: {feed.contact.booking.get('de', '')}",
        f"CTA English: {feed.contact.booking.get('en', '')}",
        f"Location German: {feed.location.get('de', '')}",
        f"Location English: {feed.location.get('en', '')}",
    ]
    if feed.vat_id:
        lines.append(f"VAT ID: {feed.vat_id}")
    return "\n".join(lines)


class WebProfileSource:
    """Reads the markdown twin and the JSON feed off the live site."""

    def __init__(self, *, base_url: str, locale: str = "en") -> None:
        self._base = base_url.rstrip("/")
        self._locale = locale

    @property
    def twin_url(self) -> str:
        return f"{self._base}/{self._locale}.md"

    @property
    def feed_url(self) -> str:
        return f"{self._base}/api/profile.json"

    async def fetch(self) -> RemoteProfile:
        """Both documents, or a :class:`ProfileUnavailableError` naming the one that failed."""
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            markdown = await self._get(client, self.twin_url)
            raw_feed = await self._get(client, self.feed_url)
        if not markdown.strip():
            raise ProfileUnavailableError(f"{self.twin_url} returned an empty profile")
        try:
            feed = ProfileFeed.model_validate_json(raw_feed)
        except ValidationError as err:
            raise ProfileUnavailableError(
                f"{self.feed_url} is not a usable profile feed: {err}"
            ) from err
        logger.info("profile fetched from %s (%d chars)", self.twin_url, len(markdown))
        return RemoteProfile(markdown=markdown, feed=feed, source_url=self.twin_url)

    @staticmethod
    @retry(
        retry=retry_if_exception_type(httpx.HTTPError),
        stop=stop_after_attempt(_ATTEMPTS),
        wait=wait_exponential_jitter(initial=1.0, max=8.0),
        reraise=True,
    )
    async def _fetch_once(client: httpx.AsyncClient, url: str) -> str:
        response = await client.get(url)
        response.raise_for_status()
        return response.text

    async def _get(self, client: httpx.AsyncClient, url: str) -> str:
        try:
            return await self._fetch_once(client, url)
        except httpx.HTTPError as err:
            raise ProfileUnavailableError(f"cannot read {url}: {err}") from err
