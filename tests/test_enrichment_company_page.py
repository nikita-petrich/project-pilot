"""The board's own company page: read first, marked as stated, and a shortcut to the site."""

from pathlib import Path

from project_pilot.enrichment.extract import outbound_site, scan_html
from project_pilot.ingestion.normalize import company_page_url

_PAGE = (Path(__file__).parent / "fixtures" / "freelancermap_company_page.html").read_text()
_BOARD_URL = (
    "https://www.freelancermap.de/firma/"
    "556-weissenberg-business-consulting-gmbh-weissenberg-delivering-excellence"
)
_LISTING_URL = "https://www.freelancermap.de/projekt/ai-und-software-developer-m-w-d"


def test_the_link_is_read_off_the_listing_not_assembled() -> None:
    # Building /firma/<id>-<company-name> ourselves would break the day the company
    # renames itself, and we would fetch a dead URL without noticing.
    raw = {
        "id": 3038296,
        "company": "Weissenberg Business Consulting GmbH",
        "companyUrl": (
            "/firma/556-weissenberg-business-consulting-gmbh-weissenberg-delivering-excellence"
        ),
    }
    assert company_page_url(raw, _LISTING_URL) == _BOARD_URL


def test_a_listing_without_a_company_link_simply_has_none() -> None:
    # No substitute procedure: an ad that names no company page gets the web search
    # it would have got anyway.
    assert company_page_url({"company": "Muster GmbH"}, _LISTING_URL) is None
    assert company_page_url({"companyUrl": ""}, _LISTING_URL) is None
    assert company_page_url({"companyUrl": 556}, _LISTING_URL) is None


def test_a_non_web_link_is_refused() -> None:
    assert company_page_url({"companyUrl": "javascript:alert(1)"}, _LISTING_URL) is None


def test_the_page_yields_the_agents_own_contact_data() -> None:
    contacts = scan_html(_PAGE)
    assert "i.strucken@weissenberg-solutions.de" in contacts.emails
    assert any("54080932" in phone for phone in contacts.phones)


def test_the_outbound_link_is_the_companys_real_homepage() -> None:
    # Following it beats searching: it is the site the agent themselves points at.
    # Tracking parameters are dropped, the board itself and LinkedIn are skipped.
    assert outbound_site(_PAGE, _BOARD_URL) == "https://www.weissenberg-solutions.de/"


def test_a_page_that_links_nowhere_useful_yields_no_site() -> None:
    html = (
        '<html><body><a href="/projekte">Projekte</a>'
        '<a href="https://www.linkedin.com/company/x">LinkedIn</a>'
        '<a href="mailto:a@b.de">Mail</a></body></html>'
    )
    assert outbound_site(html, _BOARD_URL) is None
