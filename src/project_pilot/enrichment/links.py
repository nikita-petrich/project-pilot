"""Pure builders for LinkedIn/Google research links.

These are constructed search URLs, not scraped pages: opening them is what "search
the company/person on LinkedIn" and "search on Google" mean here. Nik follows them
in his own browser session, so no automated access to either site takes place.
"""

from urllib.parse import quote_plus

from project_pilot.enrichment.schemas import DiscoveryLinks

_LINKEDIN_COMPANY = "https://www.linkedin.com/search/results/companies/?keywords="
_LINKEDIN_PEOPLE = "https://www.linkedin.com/search/results/people/?keywords="
_GOOGLE = "https://www.google.com/search?q="


def google_search_url(query: str) -> str:
    return f"{_GOOGLE}{quote_plus(query)}"


def linkedin_company_url(company: str) -> str:
    return f"{_LINKEDIN_COMPANY}{quote_plus(company)}"


def linkedin_people_url(*, company: str | None, person: str | None) -> str:
    """People search for ``person AND company`` (boolean AND narrows to the right hit)."""
    keywords = " AND ".join(part for part in (person, company) if part)
    return f"{_LINKEDIN_PEOPLE}{quote_plus(keywords)}"


def build_links(
    *, company: str | None, person: str | None, title: str | None = None
) -> DiscoveryLinks:
    """Assemble the four research links from whatever subject fields are known."""
    subject = company or person or title or ""
    return DiscoveryLinks(
        linkedin_company=linkedin_company_url(company or subject),
        linkedin_people=linkedin_people_url(company=company, person=person or title),
        google_company=google_search_url(subject),
        google_contact=google_search_url(f"{subject} Impressum Kontakt E-Mail Telefon".strip()),
    )
