"""Contact enrichment orchestration: website discovery, page reading, extraction.

Pure of any database or transport concern: it takes a company/person, reaches the
company website through the injected fetcher and search provider, and returns a
``ContactEnrichment``. A failed page fetch never breaks the lookup — the caller
always gets whatever was found plus the LinkedIn/Google research links.
"""

import logging
from urllib.parse import urlsplit, urlunsplit

from project_pilot.enrichment.chain import resolve_emails, resolve_persons, resolve_phones
from project_pilot.enrichment.extract import (
    DIRECTORY_HOSTS,
    PageContacts,
    embedded_contacts,
    embedded_site,
    find_contact_links,
    outbound_site,
    scan_html,
)
from project_pilot.enrichment.fetch import FetchedPage, Fetcher
from project_pilot.enrichment.links import build_links
from project_pilot.enrichment.message import build_connection_message
from project_pilot.enrichment.schemas import ContactEnrichment
from project_pilot.enrichment.search import SearchProvider
from project_pilot.errors import EnrichmentError

logger = logging.getLogger(__name__)


def _board_contacts(html: str) -> PageContacts:
    """What a board company page states: its links, and the record behind them.

    Both count as stated — the agency entered them on its own page either way. The
    record matters because not every page renders it into links.
    """
    links, record = scan_html(html), embedded_contacts(html)
    return PageContacts(
        emails=[*links.emails, *record.emails],
        phones=[*links.phones, *record.phones],
        persons=links.persons,
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


class EnrichmentService:
    """Finds a company's contact data from its own website (bounded, robots-aware)."""

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        search: SearchProvider,
        max_pages: int = 6,
        search_limit: int = 5,
        sender: str | None = None,
        offer_du: bool = False,
    ) -> None:
        self._fetcher = fetcher
        self._search = search
        self._max_pages = max(1, max_pages)
        self._search_limit = search_limit
        self._sender = sender
        self._offer_du = offer_du

    async def enrich(
        self,
        *,
        company: str | None,
        person: str | None = None,
        title: str | None = None,
        known_url: str | None = None,
        known_email: str | None = None,
        company_page: str | None = None,
    ) -> ContactEnrichment:
        """Look up contact data for ``company``/``person`` and assemble the result.

        ``company_page`` is the company's own page on the board the listing came
        from. It is read first: the agent maintains it themselves, so what stands
        there beats anything the Impressum crawl turns up — and its outbound link
        is the company's real homepage, which saves the website search.

        Only that page counts as stated. A ``known_email`` lifted out of the ad
        text, or a ``person`` handed in by the caller, stays marked as ``web``:
        both are plausible rather than published, and flagging them for a look is
        the safe direction for a tool that sends real mail.
        """
        if not any((company, person, title)):
            raise EnrichmentError("nothing to enrich: no company, person, or title given")

        board = await self._fetch(company_page) if company_page else None
        stated = _board_contacts(board.text) if board is not None else None
        if board is not None and not known_url:
            known_url = outbound_site(board.text, board.url) or embedded_site(board.text, board.url)

        website = self._origin(known_url) if known_url else await self._find_website(company)
        pages = await self._gather_pages(website) if website else []

        emails: list[str] = list(filter(None, [known_email]))
        phones: list[str] = []
        persons: list[str] = list(filter(None, [person]))
        for page in pages:
            contacts = scan_html(page.text)
            emails.extend(contacts.emails)
            phones.extend(contacts.phones)
            persons.extend(contacts.persons)

        return ContactEnrichment(
            company=company,
            person=person,
            website=website,
            company_page=board.url if board is not None else company_page,
            links=build_links(company=company, person=person, title=title),
            linkedin_message=build_connection_message(
                person=person,
                company=company,
                title=title,
                sender=self._sender,
                offer_du=self._offer_du,
            ),
            emails=resolve_emails(
                stated=_dedupe(stated.emails if stated else []),
                found=_dedupe(emails),
                person=person,
            ),
            phones=resolve_phones(
                stated=_dedupe(stated.phones if stated else []), found=_dedupe(phones)
            ),
            persons=resolve_persons(
                stated=_dedupe(stated.persons if stated else []), found=_dedupe(persons)
            ),
            sources=[page.url for page in ([board] if board is not None else []) + pages],
        )

    async def _find_website(self, company: str | None) -> str | None:
        if not company:
            return None
        try:
            results = await self._search.search(company, limit=self._search_limit)
        except Exception as err:  # a flaky search must not sink the whole lookup
            logger.warning("website search failed for %r: %s", company, err)
            return None
        for result in results:
            host = urlsplit(result.url).netloc.lower().removeprefix("www.")
            if host and not any(skip in host for skip in DIRECTORY_HOSTS):
                return self._origin(result.url)
        return None

    async def _gather_pages(self, website: str) -> list[FetchedPage]:
        home = await self._fetch(website)
        if home is None:
            return []
        pages = [home]
        for link in find_contact_links(home.text, home.url):
            if len(pages) >= self._max_pages:
                break
            page = await self._fetch(link.url)
            if page is not None:
                pages.append(page)
        return pages

    async def _fetch(self, url: str) -> FetchedPage | None:
        try:
            return await self._fetcher.fetch(url)
        except Exception as err:  # per-page: skip a blocked/broken page, keep the rest
            logger.info("skipping %s: %s", url, err)
            return None

    @staticmethod
    def _origin(url: str) -> str | None:
        parts = urlsplit(url if "//" in url else f"https://{url}")
        if not parts.netloc:
            return None
        return urlunsplit((parts.scheme or "https", parts.netloc, "/", "", ""))
