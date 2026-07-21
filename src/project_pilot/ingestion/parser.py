"""freelancermap list/detail HTML parsing (selectors centralized as constants)."""

from dataclasses import dataclass
from datetime import date, datetime

from bs4 import BeautifulSoup, Tag

from project_pilot.errors import SelectorMismatchError
from project_pilot.ingestion.normalize import (
    canonicalize_url,
    compute_url_hash,
    parse_end,
    parse_posted,
    parse_start,
    remote_status_from_text,
)
from project_pilot.models import PostedPrecision, RemoteStatus

# --- freelancermap selectors: the one place to adjust when the markup changes ---
LIST_CARD = "article.project-card"
LIST_TITLE_LINK = "h2.project-title a"
LIST_POSTED = ".project-posted"

DETAIL_TITLE = "h1.project-title"
DETAIL_FACTS = "dl.project-facts"
DETAIL_DESCRIPTION = "section.project-description"
DETAIL_SKILL = "ul.project-skills li"
DETAIL_POSTED_TIME = "dd.fact-posted time"

# German fact labels (lowercased, colon-stripped) looked up in the detail facts list.
FACT_POSTED = "eingetragen"
FACT_START = "projektstart"
FACT_END = "projektende"
FACT_LOCATION = "einsatzort"
FACT_REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class ListingSummary:
    """One project card from a list page: enough to dedupe and gate on freshness."""

    external_url: str
    url_hash: str
    title: str
    posted_at: datetime | None
    posted_at_precision: PostedPrecision


@dataclass(frozen=True, slots=True)
class ParsedListing:
    """A fully parsed detail page, ready to become a Listing row."""

    source: str
    external_url: str
    url_hash: str
    title: str
    description: str
    skills: list[str]
    start_date: date | None
    start_asap: bool
    end_date: date | None
    location: str | None
    remote_status: RemoteStatus
    posted_at: datetime | None
    posted_at_precision: PostedPrecision
    raw: dict[str, object]


def _text(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _attr(node: Tag | None, name: str) -> str | None:
    if node is None:
        return None
    value = node.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _parse_facts(soup: BeautifulSoup) -> dict[str, str]:
    facts: dict[str, str] = {}
    definition_list = soup.select_one(DETAIL_FACTS)
    if definition_list is None:
        return facts
    terms = definition_list.select("dt")
    definitions = definition_list.select("dd")
    for term, definition in zip(terms, definitions, strict=False):
        label = _text(term).lower().rstrip(":").strip()
        if label:
            facts[label] = _text(definition)
    return facts


def parse_list_page(html: str, base_url: str) -> list[ListingSummary]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(LIST_CARD)
    if not cards:
        raise SelectorMismatchError(f"no list cards matched {LIST_CARD!r}")

    summaries: list[ListingSummary] = []
    for card in cards:
        link = card.select_one(LIST_TITLE_LINK)
        href = _attr(link, "href")
        if link is None or href is None:
            continue
        url = canonicalize_url(href, base_url)
        posted_element = card.select_one(LIST_POSTED)
        posted_at, precision = parse_posted(
            _attr(posted_element, "datetime"), _text(posted_element)
        )
        summaries.append(
            ListingSummary(
                external_url=url,
                url_hash=compute_url_hash(url),
                title=_text(link),
                posted_at=posted_at,
                posted_at_precision=precision,
            )
        )

    if not summaries:
        raise SelectorMismatchError("list cards found but no listing links parsed")
    return summaries


def parse_detail_page(html: str, base_url: str, *, source: str, external_url: str) -> ParsedListing:
    soup = BeautifulSoup(html, "lxml")
    title_element = soup.select_one(DETAIL_TITLE)
    if title_element is None:
        raise SelectorMismatchError(f"no detail title matched {DETAIL_TITLE!r}")

    facts = _parse_facts(soup)
    skills = [text for skill in soup.select(DETAIL_SKILL) if (text := _text(skill))]
    start_date, start_asap = parse_start(facts.get(FACT_START, ""))
    location = facts.get(FACT_LOCATION) or None
    remote_source = facts.get(FACT_REMOTE) or location or ""
    posted_at, precision = parse_posted(
        _attr(soup.select_one(DETAIL_POSTED_TIME), "datetime"),
        facts.get(FACT_POSTED, ""),
    )

    url = canonicalize_url(external_url, base_url)
    return ParsedListing(
        source=source,
        external_url=url,
        url_hash=compute_url_hash(url),
        title=_text(title_element),
        description=_text(soup.select_one(DETAIL_DESCRIPTION)),
        skills=skills,
        start_date=start_date,
        start_asap=start_asap,
        end_date=parse_end(facts.get(FACT_END, "")),
        location=location,
        remote_status=remote_status_from_text(remote_source),
        posted_at=posted_at,
        posted_at_precision=precision,
        raw={"facts": facts, "skills": skills},
    )
